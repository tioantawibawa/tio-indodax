"""Agent Office: read-only snapshot of what every part of the agent is doing.

Reads the agent database with ``mode=ro`` (it can never write or trade) and the
``office:heartbeat`` state the runner stores after each cycle. Contains no
credentials: the office never loads ``.env``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from agent.config import Settings
from agent.portfolio.portfolio import Portfolio

ZERO = Decimal(0)
OPEN_ORDER_STATUSES = ("PENDING_SUBMIT", "NEW", "PARTIALLY_FILLED")


def _dec(x) -> Decimal | None:
    try:
        return Decimal(str(x)) if x is not None else None
    except Exception:  # noqa: BLE001
        return None


def _f(x) -> float | None:
    return float(x) if x is not None else None


def _age_s(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return None


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s} dtk lalu"
    if s < 5400:
        return f"{s // 60} mnt lalu"
    if s < 172800:
        return f"{s // 3600} jam lalu"
    return f"{s // 86400} hari lalu"


def _rp(x) -> str:
    if x is None:
        return "—"
    v = float(x)
    return f"{'-' if v < 0 else ''}Rp {abs(v):,.0f}".replace(",", ".")


class _RO:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row

    def state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def close(self) -> None:
        self.conn.close()


def build_state(db_path: str, s: Settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(s.reporting.timezone)
    db = _RO(db_path)
    try:
        return _build(db, s, now, tz)
    finally:
        db.close()


def _build(db: _RO, s: Settings, now: datetime, tz: ZoneInfo) -> dict:
    hb = db.state("office:heartbeat") or {}
    mode = hb.get("mode") or "paper"
    interval_s = 60 * int(hb.get("interval_min") or s.cycle.interval_minutes)
    hb_age = _age_s(hb.get("ts"), now)
    alive = hb_age is not None and hb_age <= 2 * interval_s + 60
    L = s.risk
    status = db.state(f"{mode}:status", hb.get("status") or "RUNNING")
    pause_reason = db.state(f"{mode}:pause_reason")
    capital = _dec(db.state(f"{mode}:capital")) or _dec(hb.get("capital")) or Decimal(str(L.agent_capital_idr))

    # ---- ledger: same replay as the agent (agent/portfolio/persistence.py)
    fills = db.rows("SELECT * FROM fills WHERE mode = ? ORDER BY ts, id", mode)
    pf = Portfolio(capital)
    for f in fills:
        pf.apply_fill(f["pair"], f["side"], Decimal(f["qty"]), Decimal(f["price"]), Decimal(f["fee_idr"]),
                      datetime.fromisoformat(f["ts"]), strict=False)
    stops = db.state(f"{mode}:stops", {}) or {}
    for pair, stop in stops.items():
        if pair in pf.positions:
            pf.raise_stop(pair, Decimal(stop))
    positions = pf.positions
    cash = pf.cash_idr
    pairs_hb = hb.get("pairs") or {}
    pos_out, pos_value = [], ZERO
    for pair, p in positions.items():
        bid = _dec((pairs_hb.get(pair) or {}).get("bid")) or p.avg_cost
        value = p.qty * bid
        pos_value += value
        pos_out.append({"pair": pair, "qty": str(p.qty), "avg_cost": _f(p.avg_cost), "price": _f(bid),
                        "value": _f(value), "unrealized": _f(p.unrealized(bid)),
                        "unrealized_pct": _f((bid / p.avg_cost - 1) * 100) if p.avg_cost else None,
                        "stop": _f(p.stop_loss), "opened": p.opened_at.isoformat()})
    equity = cash + pos_value
    peak = max(_dec(db.state(f"{mode}:peak_equity")) or equity, equity)
    today = now.astimezone(tz).date().isoformat()
    day = db.rows("SELECT * FROM daily_pnl WHERE mode = ? AND date = ?", mode, today)
    day_start = Decimal(day[0]["start_equity"]) if day else equity
    drawdown = (peak - equity) / peak * 100 if peak > 0 else ZERO
    day_loss = max(ZERO, (day_start - equity) / capital * 100)

    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    orders = db.rows("SELECT * FROM orders WHERE mode = ? ORDER BY id DESC LIMIT 200", mode)
    orders_today = sum(1 for o in orders if o["ts_created"] >= midnight.isoformat())
    orders_hour = sum(1 for o in orders if o["ts_created"] >= hour_ago.isoformat())
    resting = [o for o in orders if o["status"] in OPEN_ORDER_STATUSES]
    decisions = db.rows("SELECT * FROM decisions WHERE mode = ? ORDER BY id DESC LIMIT 30", mode)
    dec_today = [d for d in decisions if d["ts"] >= midnight.isoformat()]
    vetoes_today = [d for d in dec_today if d["verdict"] == "VETO"]
    errors = db.rows("SELECT * FROM errors ORDER BY id DESC LIMIT 15")
    errors_24h = [e for e in errors if e["ts"] >= (now - timedelta(hours=24)).isoformat()]
    last_report = db.state("office:last_report")

    # ---- signal radar
    radar = []
    for pair in s.market.whitelist:
        d = pairs_hb.get(pair) or {}
        radar.append({"pair": pair, "close": d.get("close"), "breakout": d.get("breakout"),
                      "gap_pct": d.get("gap_pct"), "above_ema": d.get("above_ema"), "held": pair in positions,
                      "regime": d.get("regime"), "note": d.get("note", "")})

    # ---- desks: who is doing what
    desks = []
    hb_local = datetime.fromisoformat(hb["ts"]).astimezone(tz).strftime("%H:%M") if hb.get("ts") else "—"
    if not hb:
        desks.append(_desk("market", "📡", "Pengamat Pasar", "Data harga & orderbook", "down",
                           "Belum ada siklus tercatat — agent belum pernah jalan dengan versi ini.", ""))
    elif alive:
        desks.append(_desk("market", "📡", "Pengamat Pasar", "Data harga & orderbook", "working",
                           f"Memantau {len(s.market.whitelist)} pair · siklus terakhir {hb_local} ({_ago(hb_age)})",
                           f"Siklus tiap {interval_s // 60} menit · candle harian ditutup 07:00 WIB"))
    else:
        desks.append(_desk("market", "📡", "Pengamat Pasar", "Data harga & orderbook", "down",
                           f"Tidak ada siklus sejak {hb_local} ({_ago(hb_age)}) — agent berhenti?",
                           "Cek: sudo systemctl status indodax-agent"))

    approved_today = [d for d in dec_today if d["verdict"] in ("APPROVE", "RESIZE") and d["side"] == "buy"]
    near = [r for r in radar if r["gap_pct"] is not None and not r["held"] and r["above_ema"]]
    near.sort(key=lambda r: r["gap_pct"])
    if approved_today:
        a = approved_today[0]
        analyst = ("working", f"Sinyal beli {a['pair'].upper()} disetujui hari ini", a["proposal_reason"] or "")
    elif positions:
        analyst = ("working", f"Mengawal {len(positions)} posisi dengan trailing stop",
                   f"Terdekat ke breakout: {near[0]['pair'].split('_')[0].upper()} +{near[0]['gap_pct']:.1f}%"
                   if near else "Tidak ada kandidat breakout lain")
    elif near:
        analyst = ("idle", f"Menunggu breakout · terdekat {near[0]['pair'].split('_')[0].upper()} "
                           f"+{near[0]['gap_pct']:.1f}%",
                   "Beli bila close harian > high 20 hari & di atas EMA100")
    else:
        analyst = ("idle", "Tidak ada koin dalam tren naik — tidak mencari entry", "")
    desks.append(_desk("analyst", "🧠", "Analis Strategi", "Sinyal trend-following harian", *analyst))

    risk_detail = (f"Drawdown {drawdown:.2f}% / {L.max_drawdown_pct}% · rugi hari ini {day_loss:.2f}% / "
                   f"{L.daily_loss_limit_pct}% · veto hari ini {len(vetoes_today)}")
    if status == "HALTED":
        risk = ("down", f"KILL SWITCH aktif — HALTED ({pause_reason or '—'}) · perlu /resume")
    elif status == "PAUSED":
        risk = ("warn", f"Entry dihentikan — PAUSED ({pause_reason or '—'})")
    elif hb and hb.get("reconcile_ok") is False:
        risk = ("warn", "Rekonsiliasi tidak cocok — semua order diblokir")
    else:
        risk = ("working" if alive else "idle", f"Semua hard limit dijaga · status {status}")
    desks.append(_desk("risk", "🛡️", "Manajer Risiko", "Hard limits & kill switch", *risk, risk_detail))

    last_fill = fills[-1] if fills else None
    exec_detail = (f"Fill terakhir: {last_fill['side'].upper()} {last_fill['pair']} "
                   f"{_ago(_age_s(last_fill['ts'], now))}" if last_fill else "Belum ada fill")
    if resting:
        execu = ("working", f"Menjaga {len(resting)} order terbuka")
    else:
        execu = ("idle", "Tidak ada order terbuka" + (" (simulasi paper)" if mode == "paper" else ""))
    desks.append(_desk("executor", "⚡", "Eksekutor", "Limit order di Indodax" if mode == "live" else
                       "Simulasi order (paper)", *execu,
                       f"{exec_detail} · order hari ini {orders_today}/{L.max_orders_per_day}"))

    dm = hb.get("deadman")
    if mode != "live" or dm is None:
        dead = ("off", "Tidak aktif di mode paper", "Aktif otomatis di mode live")
    else:
        beat_age = (now.timestamp() - dm["last_ok_at"]) if dm.get("last_ok_at") else None
        if not dm.get("healthy"):
            dead = ("down", f"Heartbeat gagal {dm.get('failures')}× — entry dihentikan", "")
        elif beat_age is not None and beat_age <= 3 * s.deadman.heartbeat_s:
            dead = ("working", f"Heartbeat diterima {_ago(beat_age)}",
                    f"Bila agent mati, Indodax membatalkan semua order dalam {s.deadman.countdown_ms // 1000} dtk")
        else:
            dead = ("warn", f"Heartbeat terakhir {_ago(beat_age)}", "")
    desks.append(_desk("deadman", "⏱️", "Penjaga Deadman", "countdownCancelAll", *dead))

    tg = hb.get("telegram")
    rep_txt = f"Laporan harian terakhir {_ago(_age_s(last_report, now))}" if last_report else "Belum ada laporan harian"
    if tg is False:
        rep = ("warn", "Telegram terputus — mencoba tersambung lagi", rep_txt)
    else:
        rep = ("idle", f"Laporan berikutnya {s.reporting.daily_report_time} WIB", rep_txt)
    desks.append(_desk("reporter", "📨", "Pelapor", "Telegram & laporan harian", *rep))

    pm = hb.get("pm")
    if not pm:
        desks.append(_desk("portfolio", "💼", "Manajer Portofolio", "Alokasi modal mingguan", "off",
                           "Tidak aktif (portfolio.enabled = false)", ""))
        desks.append(_desk("reviewer", "🔍", "Reviewer Portofolio", "Review kualitas model", "off",
                           "Tidak aktif", ""))
    else:
        w = pm.get("weights") or {}
        held = [k.split("_")[0].upper() for k, v in sorted(w.items(), key=lambda kv: -kv[1]) if v > 0]
        tot = sum(w.values()) * 100
        plan_age = _age_s(pm.get("plan_at"), now)
        nxt = (datetime.fromisoformat(pm["plan_at"]) + timedelta(days=pm.get("rebalance_days") or 7)
               ).astimezone(tz).strftime("%d %b") if pm.get("plan_at") else "—"
        if pm.get("plan_at") is None:
            pmd = ("idle", "Menyiapkan rencana alokasi pertama", "")
        elif plan_age is not None and plan_age < 36 * 3600 and (resting or dec_today):
            pmd = ("working", f"Rebalance berjalan: target {tot:.0f}% di {', '.join(held) or 'kas'}",
                   f"Target volatilitas {pm.get('target_vol', 0) * 100:.0f}% · skala {pm.get('scale') or 0:.2f}")
        else:
            pmd = ("idle", f"Target terinvestasi {tot:.0f}% ({', '.join(held) or 'semua kas'})",
                   f"Rebalance berikutnya ±{nxt} · target volatilitas {pm.get('target_vol', 0) * 100:.0f}%")
        desks.append(_desk("portfolio", "💼", "Manajer Portofolio", "Alokasi modal mingguan", *pmd))
        rv = pm.get("review")
        if not rv:
            rvd = ("idle", "Belum ada review — terjadwal mingguan", "Kirim /review untuk review sekarang")
        else:
            st = {"BAIK": "idle", "AWAL": "idle", "PERHATIAN": "warn", "BURUK": "down"}.get(rv["verdict"], "idle")
            rvd = (st, f"Review terakhir: {rv['verdict']} ({_ago(_age_s(rv.get('at'), now))})",
                   (rv.get("findings") or [""])[0][:120])
        desks.append(_desk("reviewer", "🔍", "Reviewer Portofolio", "Review kualitas model", *rvd))
        k_up = sum(1 for r in radar if r["above_ema"])
        for d in desks:
            if d["key"] == "analyst" and not _keeps_alert(d):
                d.update(state="working" if alive else "idle",
                         task=f"{k_up}/{len(radar)} koin dalam tren naik (di atas EMA{s.portfolio.trend_ema})",
                         detail="Sinyal tren untuk manajer portofolio")

    # ---- equity curve (immune to owner capital changes: capital + cumulative daily PnL)
    curve, cum = [], ZERO
    for d in db.rows("SELECT * FROM daily_pnl WHERE mode = ? ORDER BY date", mode):
        if d["end_equity"] is not None:
            cum += Decimal(d["end_equity"]) - Decimal(d["start_equity"])
            curve.append([d["date"], _f(capital + cum)])

    def local(ts):
        return datetime.fromisoformat(ts).astimezone(tz).strftime("%d %b %H:%M")

    return {
        "generated_at": now.isoformat(), "mode": mode, "alive": alive, "heartbeat_age_s": hb_age,
        "heartbeat_ago": _ago(hb_age), "status": status, "pause_reason": pause_reason,
        "uptime_s": _age_s(hb.get("started_at"), now) if alive else None,
        "kpi": {"capital": _f(capital), "equity": _f(equity), "cash": _f(cash), "peak": _f(peak),
                "day_pnl": _f(equity - day_start), "day_pnl_pct": _f((equity - day_start) / capital * 100),
                "total_pnl": _f(equity - capital), "total_pnl_pct": _f((equity - capital) / capital * 100),
                "drawdown_pct": _f(drawdown), "day_loss_pct": _f(day_loss),
                "exposure_pct": _f(pos_value / capital * 100), "positions": len(positions),
                "max_positions": L.max_open_positions, "orders_today": orders_today,
                "orders_hour": orders_hour, "exchange_free_idr": _f(_dec(hb.get("exchange_free_idr"))),
                "clock_offset_ms": hb.get("clock_offset_ms")},
        "limits": {"max_drawdown_pct": L.max_drawdown_pct, "daily_loss_limit_pct": L.daily_loss_limit_pct,
                   "max_position_pct": L.max_position_pct, "max_orders_per_day": L.max_orders_per_day,
                   "max_orders_per_hour": L.max_orders_per_hour},
        "desks": desks,
        "radar": radar,
        "positions": pos_out,
        "orders": [{"time": local(o["ts_created"]), "pair": o["pair"], "side": o["side"], "price": _f(_dec(o["price"])),
                    "qty": o["qty"], "status": o["status"]} for o in resting],
        "decisions": [{"time": local(d["ts"]), "pair": d["pair"], "side": d["side"], "intent": d["intent"],
                       "verdict": d["verdict"], "price": _f(_dec(d["price"])),
                       "reason": (json.loads(d["reasons_json"]) or [""])[0][:140],
                       "setup": d["setup"] or ""} for d in decisions[:20]],
        "fills": [{"time": local(f["ts"]), "pair": f["pair"], "side": f["side"], "qty": f["qty"],
                   "price": _f(Decimal(f["price"])), "fee": _f(Decimal(f["fee_idr"])),
                   "pnl": _f(_dec(f["realized_pnl"]))} for f in list(reversed(fills))[:20]],
        "errors": [{"time": local(e["ts"]), "component": e["component"], "message": e["message"][:160]}
                   for e in errors[:10]],
        "errors_24h": len(errors_24h),
        "equity_curve": curve[-120:],
        "whitelist": list(s.market.whitelist),
    }


def _keeps_alert(d: dict) -> bool:
    return d.get("state") in ("warn", "down")


def _desk(key, icon, name, role, state, task, detail="") -> dict:
    return {"key": key, "icon": icon, "name": name, "role": role, "state": state, "task": task, "detail": detail}
