"""Portfolio manager: weekly trend-filtered, volatility-targeted allocation (owner choice 2026-10-07).

Model (research in docs/portfolio_manager.md, validated in the real backtester):
1. Universe = whitelist. A coin is eligible when its last daily close > EMA(``trend_ema``).
2. Eligible coins get inverse-volatility weights (vol = stdev of daily returns, ``vol_lookback`` days).
3. The whole book is scaled so the portfolio volatility (full covariance, ``cov_lookback`` days,
   annualised) equals ``target_vol``; never levered (scale <= 1). Per-coin cap ``max_weight``.
4. Every ``rebalance_days`` a plan (target weights) is made. Sells go first; buys only in a later
   cycle, after the sells have settled, so a buy never relies on cash that is not there yet.
   Trades smaller than the band (``band_pct`` of the base, at least ``min_trade_idr``) are skipped.

The manager only *proposes*. Every order still goes through the risk manager (hard limits, costs,
exchange minimum, stop-loss rule). Each position carries a fixed catastrophe stop
(entry - ``stop_atr_mult`` x ATR, not trailed); the weekly trend filter is the normal exit.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from agent.analysis.signals import TimeframeFeatures
from agent.config import PortfolioSettings
from agent.data.market_data import PairMarket
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import EXIT_MIN_MARGIN
from agent.storage.db import Database
from agent.strategy.strategy import TradeProposal

ZERO = Decimal(0)
PLAN_KEY = "{mode}:pm:plan"
CHECK_KEY = "{mode}:pm:last_check"
HISTORY_KEY = "{mode}:pm:history"


def _d(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def target_weights(closes: Mapping[str, pd.Series], p: PortfolioSettings) -> tuple[dict[str, float], dict]:
    """Target weight per pair (sum <= 1; the rest is cash) and diagnostics. Pure function."""
    diag: dict = {"eligible": [], "excluded": {}}
    eligible, rets = [], {}
    need = max(p.trend_ema, p.cov_lookback, p.vol_lookback) + 2
    for pair, c in closes.items():
        c = c.dropna()
        if len(c) < need:
            diag["excluded"][pair] = f"riwayat {len(c)} hari < {need}"
            continue
        ema = c.ewm(span=p.trend_ema, adjust=False).mean().iloc[-1]
        if not c.iloc[-1] > ema:
            diag["excluded"][pair] = f"di bawah EMA{p.trend_ema}"
            continue
        eligible.append(pair)
        rets[pair] = c.pct_change()
    weights = {pair: 0.0 for pair in closes}
    if not eligible:
        diag.update(scale=0.0, port_vol=None)
        return weights, diag
    r = pd.DataFrame(rets).dropna()
    vol = r.tail(p.vol_lookback).std()
    if (vol <= 0).any() or vol.isna().any():
        diag.update(scale=0.0, port_vol=None, error="volatilitas tidak valid")
        return weights, diag
    iv = 1 / vol
    w = iv / iv.sum()
    cov = r.tail(p.cov_lookback).cov().values * 365
    port_vol = float(math.sqrt(max(w.values @ cov @ w.values, 0.0)))
    scale = min(1.0, p.target_vol / port_vol) if port_vol > 0 else 0.0
    for pair in eligible:
        weights[pair] = min(float(w[pair] * scale), p.max_weight)
    diag.update(eligible=eligible, scale=scale, port_vol=port_vol,
                vol={k: float(v * math.sqrt(365)) for k, v in vol.items()})
    return weights, diag


@dataclass
class PMResult:
    proposals: list[TradeProposal]
    notes: dict[str, str]
    plan: dict | None


class PortfolioManager:
    def __init__(self, settings: PortfolioSettings, db: Database, mode: str, capital_idr: Decimal,
                 whitelist: tuple[str, ...], emergency_slippage_pct: float = 1.0):
        self.p, self.db, self.mode, self.capital = settings, db, mode, capital_idr
        self.whitelist = tuple(whitelist)
        self.emergency_slippage = _d(emergency_slippage_pct)

    # ------------------------------------------------------------- planning

    def plan(self) -> dict | None:
        return self.db.get_state(PLAN_KEY.format(mode=self.mode))

    def _due(self, now: datetime, plan: dict | None) -> bool:
        if plan is None:
            return True
        made = datetime.fromisoformat(plan["made_at"])
        return now - made >= timedelta(days=self.p.rebalance_days) - timedelta(hours=1)

    @staticmethod
    def _closes(markets: Mapping[str, PairMarket]) -> dict[str, pd.Series]:
        closes = {}
        for pair, m in markets.items():
            df = m.candles.get("1D")
            if df is not None and len(df):
                closes[pair] = df["close"].astype(float)
        return closes

    def _changes(self, old: dict, new: dict[str, float], base: Decimal) -> list[str]:
        """Human-readable differences between the current plan and a fresh recommendation."""
        band = max(_d(self.p.min_trade_idr), base * _d(self.p.band_pct) / 100)
        out = []
        for pair in sorted(set(old) | set(new)):
            a, b = old.get(pair, 0.0), new.get(pair, 0.0)
            sym = pair.split("_")[0].upper()
            if a == 0 and b > 0:
                out.append(f"{sym} masuk tren (0% → {b * 100:.0f}%)")
            elif a > 0 and b == 0:
                out.append(f"{sym} keluar tren ({a * 100:.0f}% → 0%)")
            elif abs(_d(b - a)) * base >= band:
                out.append(f"{sym} {a * 100:.0f}% → {b * 100:.0f}%")
        return out

    def _make_plan(self, markets: Mapping[str, PairMarket], pf: Portfolio, marks, now: datetime,
                   reason: str = "terjadwal", changes: list[str] | None = None) -> dict:
        weights, diag = target_weights(self._closes(markets), self.p)
        equity = pf.equity(marks)
        plan = {"made_at": now.isoformat(), "weights": weights, "equity": str(equity),
                "reason": reason, "changes": changes or [],
                "exposure_before": float(sum(pos.value(marks[k]) for k, pos in pf.positions.items()) / equity)
                if equity > 0 else 0.0,
                "scale": diag.get("scale"), "port_vol": diag.get("port_vol"), "excluded": diag.get("excluded"),
                "vol": diag.get("vol")}
        self.db.set_state(PLAN_KEY.format(mode=self.mode), plan)
        hist = self.db.get_state(HISTORY_KEY.format(mode=self.mode), []) or []
        hist.append({k: plan[k] for k in ("made_at", "weights", "equity", "exposure_before", "scale", "port_vol",
                                          "reason")})
        self.db.set_state(HISTORY_KEY.format(mode=self.mode), hist[-200:])
        return plan

    # ------------------------------------------------------------ proposals

    def propose(self, markets: Mapping[str, PairMarket], pf: Portfolio, now: datetime,
                features: Mapping[str, Mapping[str, TimeframeFeatures]], tf: str,
                pending_buy_idr: Mapping[str, Decimal]) -> PMResult:
        marks = {k: (m.orderbook.best_bid or m.ticker.last) for k, m in markets.items()}
        notes: dict[str, str] = {}
        plan = self.plan()
        complete = bool(markets) and all(k in markets for k in pf.positions) \
            and all("1D" in m.candles and len(m.candles["1D"]) for m in markets.values())
        reason, changes = None, None
        if plan is None:
            reason = "rencana pertama"
        elif self._due(now, plan):
            reason = f"terjadwal (tiap {self.p.rebalance_days} hari)"
        elif self.p.react_to_changes and complete:
            # once per new daily close: did the recommendation change since the current plan?
            last_close = max(m.candles["1D"].index[-1] for m in markets.values()).isoformat()
            if self.db.get_state(CHECK_KEY.format(mode=self.mode)) != last_close:
                self.db.set_state(CHECK_KEY.format(mode=self.mode), last_close)
                fresh, _ = target_weights(self._closes(markets), self.p)
                changes = self._changes(plan["weights"], fresh, min(pf.equity(marks), self.capital))
                if changes:
                    reason = "perubahan rekomendasi"
        if reason is not None:
            # never plan without data for a held coin (it would get weight 0 and be sold by mistake)
            if not complete:
                return PMResult([], {"*": "data pasar belum lengkap — rebalance ditunda"}, plan)
            if changes is None and plan is not None:
                fresh, _ = target_weights(self._closes(markets), self.p)
                changes = self._changes(plan["weights"], fresh, min(pf.equity(marks), self.capital))
            plan = self._make_plan(markets, pf, marks, now, reason, changes)
        if now - datetime.fromisoformat(plan["made_at"]) > timedelta(hours=self.p.plan_valid_hours):
            for k in markets:
                notes[k] = f"menunggu rebalance berikutnya (target {plan['weights'].get(k, 0) * 100:.0f}%)"
            return PMResult([], notes, plan)

        equity = pf.equity(marks)
        base = min(equity, self.capital)
        band = max(_d(self.p.min_trade_idr), base * _d(self.p.band_pct) / 100)
        sells, buys = [], []
        for pair, w in plan["weights"].items():
            m = markets.get(pair)
            if m is None or m.orderbook.best_bid is None or m.orderbook.best_ask is None:
                notes[pair] = "tidak ada orderbook"
                continue
            held = pf.positions.get(pair)
            cur = held.value(marks[pair]) if held else ZERO
            cur += pending_buy_idr.get(pair, ZERO)
            target = base * _d(w)
            delta = target - cur
            info = m.info
            if delta < 0 and held is not None and (w == 0 or -delta >= band):
                bid = m.orderbook.best_bid
                qty = held.qty if w == 0 else min(held.qty, info.round_qty(-delta / bid))
                price = info.round_price(bid, "buy")
                if qty > 0 and info.min_order_violation(price, qty):
                    notes[pair] = f"trim {qty} di bawah minimum order — dilewati"
                elif qty > 0:
                    sells.append(TradeProposal(
                        pair=pair, side="sell", order_type="limit", price=price, qty=qty,
                        intent="exit", confidence=1.0, setup="pm_exit" if w == 0 else "pm_trim",
                        reason=f"Rebalance: bobot target {w * 100:.1f}% (nilai {cur:,.0f} -> {target:,.0f})"))
            elif delta >= band:
                f = (features.get(pair) or {}).get(tf)
                if f is None or not (f.atr > 0):
                    notes[pair] = "indikator belum siap"
                    continue
                ask = m.orderbook.best_ask
                entry = info.round_price(ask, "sell")
                atr = _d(f.atr)
                # catastrophe stop only (the weekly trend filter is the real exit): fixed, wide, not trailed
                sl = info.round_price(entry - _d(self.p.stop_atr_mult) * atr, "buy")
                held_stop = held.stop_loss if held is not None else None
                if held_stop is not None and held_stop > sl:
                    sl = held_stop          # adding to a position never loosens its stop
                qty = info.round_qty(delta / entry)
                if qty <= 0 or sl <= 0 or info.min_order_violation(entry, qty):
                    notes[pair] = "tambahan di bawah minimum order — dilewati"
                    continue
                # same rule as the risk manager: the whole position must stay sellable at its stop
                total = qty + (held.qty if held is not None else ZERO)
                exit_val = total * sl * (1 - self.emergency_slippage / 100)
                if info.min_quote and exit_val < info.min_quote * EXIT_MIN_MARGIN:
                    notes[pair] = (f"target {w * 100:.0f}% ({target:,.0f}) terlalu kecil: nilai di stop darurat "
                                   f"{exit_val:,.0f} < {EXIT_MIN_MARGIN} x minimum order — dilewati")
                    continue
                buys.append(TradeProposal(
                    pair=pair, side="buy", order_type="limit", price=entry, qty=qty, intent="entry",
                    confidence=0.6, stop_loss=sl, target=info.round_price(entry + 4 * atr, "sell"),
                    setup="pm_buy",
                    reason=f"Rebalance: bobot target {w * 100:.1f}% (nilai {cur:,.0f} -> {target:,.0f})"))
            else:
                notes[pair] = (f"sesuai target {w * 100:.0f}% (selisih {delta:,.0f} < ambang {band:,.0f})"
                               if w > 0 else "target 0% — tidak dipegang")
        if sells:
            for b in buys:
                notes[b.pair] = "beli menunggu penjualan selesai"
            return PMResult(sells, notes, plan)
        return PMResult(buys, notes, plan)


def exposure(pf: Portfolio, marks: Mapping[str, Decimal]) -> float:
    eq = pf.equity(marks)
    return float(sum(p.value(marks[k]) for k, p in pf.positions.items()) / eq) if eq > 0 else 0.0
