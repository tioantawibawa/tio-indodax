"""Portfolio reviewer: periodic quality & performance check of the portfolio-manager model.

Runs weekly (``portfolio.review_weekday`` / ``review_time``) and on ``/review``. It never changes
settings — it reports a verdict with findings and recommendations; the owner decides.

Measured since the manager started (first plan) and over the last 28 days:
- actual return, max drawdown, realised volatility vs ``target_vol``
- capital utilisation (exposure at each rebalance and now)
- fees per 30 days, turnover
- model fidelity: actual return vs a *shadow* simulation of the same model on the same prices
  (a large negative gap means execution problems: vetoes, minimums, slippage, outages)
- benchmarks: equal-weight hold of the whitelist and BTC hold over the same window

Verdict rules (pre-defined, thresholds in config):
  BURUK     drawdown >= review_bad_drawdown_pct, or gap <= -review_bad_gap_pp
  PERHATIAN drawdown >= review_warn_drawdown_pct, or gap <= -review_max_gap_pp, or realised vol >
            review_max_vol_ratio x target (>= 14 days of data), or fees/30d > review_max_fees_pct_month
  BAIK      otherwise;  AWAL when there are fewer than 7 days of data
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from agent.config import PortfolioSettings
from agent.data.market_data import PairMarket
from agent.portfolio.manager import HISTORY_KEY, PLAN_KEY, target_weights
from agent.storage.db import Database

TRADE_COST = 0.00342 + 0.001   # fee+tax+clearing + spread/slippage per traded IDR (shadow model)
REVIEW_KEY = "{mode}:pm:review"


@dataclass
class Review:
    verdict: str
    at: str
    days: int
    metrics: dict = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "at": self.at, "days": self.days, "metrics": self.metrics,
                "findings": self.findings, "recommendations": self.recommendations}


def shadow_return(closes: pd.DataFrame, p: PortfolioSettings, start: pd.Timestamp) -> float | None:
    """Return (%) of the pure model, rebalanced every ``rebalance_days`` from ``start``, with costs."""
    closes = closes.sort_index()
    days = closes.index[closes.index >= start]
    if len(days) < 2:
        return None
    rets = closes.pct_change()
    w = pd.Series(0.0, index=closes.columns)
    eq, last_rb = 1.0, None
    for i, t in enumerate(days):
        if i > 0:
            r = rets.loc[t].fillna(0.0)
            gross = 1 + float((w * r).sum())
            w = w * (1 + r) / gross if gross > 0 else w
            eq *= gross
        if last_rb is None or (t - last_rb).days >= p.rebalance_days:
            hist = {c: closes.loc[:t, c].dropna() for c in closes.columns}
            target, _ = target_weights(hist, p)
            tgt = pd.Series(target).reindex(closes.columns).fillna(0.0)
            eq -= eq * float((tgt - w).abs().sum()) * TRADE_COST
            w, last_rb = tgt, t
    return (eq - 1) * 100


class PortfolioReviewer:
    def __init__(self, p: PortfolioSettings, db: Database, mode: str, capital_idr: Decimal, tz):
        self.p, self.db, self.mode, self.capital, self.tz = p, db, mode, capital_idr, tz

    def last(self) -> dict | None:
        return self.db.get_state(REVIEW_KEY.format(mode=self.mode))

    def review(self, now: datetime, markets: Mapping[str, PairMarket]) -> Review:
        hist = self.db.get_state(HISTORY_KEY.format(mode=self.mode), []) or []
        plan = self.db.get_state(PLAN_KEY.format(mode=self.mode))
        if not hist:
            rev = Review("AWAL", now.isoformat(), 0, findings=["Manajer portofolio belum membuat rencana alokasi."])
            self._store(rev)
            return rev
        start = datetime.fromisoformat(hist[0]["made_at"])
        days = max(0, (now - start).days)
        m: dict = {"since": start.astimezone(self.tz).date().isoformat(), "rebalances": len(hist)}

        # ---- equity curve since start (capital-change proof: capital + cumulative daily PnL)
        start_day = start.astimezone(self.tz).date().isoformat()
        rows = self.db._conn.execute(
            "SELECT date, start_equity, end_equity FROM daily_pnl WHERE mode = ? AND date >= ? ORDER BY date",
            (self.mode, start_day)).fetchall()
        curve, cum = [], 0.0
        for r in rows:
            if r["end_equity"] is not None:
                cum += float(r["end_equity"]) - float(r["start_equity"])
                curve.append(float(self.capital) + cum)
        cap = float(self.capital)
        if curve:
            eq = pd.Series([cap] + curve)
            m["return_pct"] = (eq.iloc[-1] / cap - 1) * 100
            m["max_drawdown_pct"] = float(((eq.cummax() - eq) / eq.cummax()).max() * 100)
            daily = eq.pct_change().dropna()
            m["realised_vol_pct"] = float(daily.std() * math.sqrt(365) * 100) if len(daily) >= 14 else None
            last28 = pd.Series(curve[-29:]) if len(curve) >= 2 else None
            m["return_28d_pct"] = float((last28.iloc[-1] / last28.iloc[0] - 1) * 100) if last28 is not None else None
        # ---- utilisation, fees, turnover
        m["avg_exposure_pct"] = sum(h.get("exposure_before") or 0 for h in hist) / len(hist) * 100
        m["target_exposure_pct"] = sum((plan or {}).get("weights", {}).values()) * 100
        fills = self.db._conn.execute("SELECT qty, price, fee_idr FROM fills WHERE mode = ? AND ts >= ?",
                                      (self.mode, start.isoformat())).fetchall()
        fees = sum(float(f["fee_idr"]) for f in fills)
        traded = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        m["fees_idr"] = fees
        m["fees_pct_month"] = fees / cap * 100 * 30 / max(days, 1)
        m["turnover_x"] = traded / cap
        # ---- shadow model and benchmarks over the same window
        closes = pd.DataFrame({k: mk.candles["1D"]["close"].astype(float)
                               for k, mk in markets.items() if "1D" in mk.candles})
        if not closes.empty:
            closes.index = pd.to_datetime(closes.index, utc=True)
            st = pd.Timestamp(start).tz_convert("UTC").normalize() - pd.Timedelta(days=1)
            m["shadow_return_pct"] = shadow_return(closes, self.p, st)
            win = closes[closes.index >= st]
            if len(win) >= 2:
                first, lastc = win.ffill().iloc[0], win.ffill().iloc[-1]
                m["bench_equal_weight_pct"] = float(((lastc / first) - 1).mean() * 100)
                if "btc_idr" in win:
                    m["bench_btc_pct"] = float((lastc["btc_idr"] / first["btc_idr"] - 1) * 100)
        if m.get("return_pct") is not None and m.get("shadow_return_pct") is not None:
            m["gap_pp"] = m["return_pct"] - m["shadow_return_pct"]
        return self._judge(now, days, m)

    def _judge(self, now: datetime, days: int, m: dict) -> Review:
        p = self.p
        findings, recs, verdict = [], [], "BAIK"
        if days < 7 or m.get("return_pct") is None:
            rev = Review("AWAL", now.isoformat(), days, m,
                         [f"Baru {days} hari berjalan — terlalu awal untuk menilai kinerja."],
                         ["Tunggu minimal 4 minggu sebelum menyimpulkan apa pun."])
            self._store(rev)
            return rev

        def warn(text, rec):
            nonlocal verdict
            findings.append(text)
            recs.append(rec)
            if verdict == "BAIK":
                verdict = "PERHATIAN"

        dd, gap, vol = m.get("max_drawdown_pct", 0), m.get("gap_pp"), m.get("realised_vol_pct")
        if dd >= p.review_bad_drawdown_pct or (gap is not None and gap <= -p.review_bad_gap_pp):
            verdict = "BURUK"
        if dd >= p.review_warn_drawdown_pct:
            warn(f"Drawdown {dd:.1f}% (batas perhatian {p.review_warn_drawdown_pct:.0f}%).",
                 "Pertimbangkan menurunkan target volatilitas (mis. 20% → 15%).")
        if gap is not None and gap <= -p.review_max_gap_pp:
            warn(f"Hasil nyata tertinggal {abs(gap):.1f} poin dari simulasi model — eksekusi tidak setia.",
                 "Periksa veto risk manager, order minimum, slippage, dan gangguan agent di log.")
        if vol is not None and vol > p.review_max_vol_ratio * p.target_vol * 100:
            warn(f"Volatilitas nyata {vol:.0f}% vs target {p.target_vol * 100:.0f}%.",
                 "Model meremehkan risiko pasar sekarang; pertimbangkan target volatilitas lebih rendah.")
        if m.get("fees_pct_month", 0) > p.review_max_fees_pct_month:
            warn(f"Biaya {m['fees_pct_month']:.2f}% modal per 30 hari (batas {p.review_max_fees_pct_month:.1f}%).",
                 "Perbesar ambang rebalance (band_pct) atau perpanjang interval rebalance.")
        if not findings:
            findings.append("Semua indikator dalam batas: risiko, biaya, dan kesetiaan eksekusi sesuai model.")
        if m.get("avg_exposure_pct", 0) < 15:
            findings.append(f"Pemakaian modal rata-rata {m['avg_exposure_pct']:.0f}% — rendah karena sedikit koin "
                            "dalam tren naik (perilaku model yang disengaja).")
        rev = Review(verdict, now.isoformat(), days, m, findings, recs)
        self._store(rev)
        return rev

    def _store(self, rev: Review) -> None:
        self.db.set_state(REVIEW_KEY.format(mode=self.mode), rev.as_dict())
        key = REVIEW_KEY.format(mode=self.mode) + ":history"
        hist = self.db.get_state(key, []) or []
        hist.append({"at": rev.at, "verdict": rev.verdict, "return_pct": rev.metrics.get("return_pct"),
                     "gap_pp": rev.metrics.get("gap_pp")})
        self.db.set_state(key, hist[-104:])


def format_review(rev: Review, esc) -> str:
    m = rev.metrics
    icon = {"BAIK": "✅", "PERHATIAN": "⚠️", "BURUK": "🛑", "AWAL": "🕐"}.get(rev.verdict, "•")

    def pct(k, d=1):
        v = m.get(k)
        return "—" if v is None else f"{v:+.{d}f}%"

    lines = [f"🔍 <b>Review manajer portofolio</b> — {icon} <b>{rev.verdict}</b> ({rev.days} hari)"]
    if m.get("return_pct") is not None:
        lines += [
            f"Return {pct('return_pct')} · 28 hari {pct('return_28d_pct')} · drawdown maks "
            f"{m.get('max_drawdown_pct', 0):.1f}%",
            f"Model bayangan {pct('shadow_return_pct')} (selisih {m['gap_pp']:+.1f} poin)"
            if m.get("gap_pp") is not None else "Model bayangan —",
            f"Pembanding: 6 koin tahan {pct('bench_equal_weight_pct')} · BTC {pct('bench_btc_pct')}",
            f"Volatilitas nyata {m['realised_vol_pct']:.0f}%" if m.get("realised_vol_pct") is not None
            else "Volatilitas nyata — (data < 14 hari)",
            f"Pemakaian modal rata-rata {m.get('avg_exposure_pct', 0):.0f}% · target sekarang "
            f"{m.get('target_exposure_pct', 0):.0f}% · biaya {m.get('fees_pct_month', 0):.2f}%/30 hari",
        ]
    lines.append("<b>Temuan</b>")
    lines += [f"• {esc(f)}" for f in rev.findings]
    if rev.recommendations:
        lines.append("<b>Rekomendasi</b> (tidak dijalankan otomatis — keputusan Anda)")
        lines += [f"• {esc(r)}" for r in rev.recommendations]
    return "\n".join(lines)
