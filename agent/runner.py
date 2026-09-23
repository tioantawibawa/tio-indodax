"""Agent runner: one object that owns the live loop state.

cycle: fetch market data -> resting paper orders -> DecisionEngine ->
paper broker -> status transitions -> DB bookkeeping -> Telegram alerts.

State that must survive a crash lives in SQLite (fills -> portfolio,
stops, status, peak equity, daily PnL); see agent/portfolio/persistence.py.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import inspect
import json
import re

import structlog

from agent.analysis.costs import CostModel
from agent.analysis.regime import Regime
from agent.analysis.signals import TimeframeFeatures
from agent.backtest.paper_broker import FillEvent, PaperBroker
from agent.config import Settings
from agent.data.market_data import PairMarket
from agent.engine import DecisionEngine
from agent.execution.executor import LiveExecutor
from agent.execution.reconcile import reconcile
from agent.portfolio.persistence import rebuild_portfolio, save_stops
from agent.reporting.daily_report import FillLine, PositionLine, ReportData, build_daily_report, rp
from agent.reporting.telegram_bot import Notifier, NullNotifier, esc
from agent.risk.risk_manager import AgentStatus, Verdict
from agent.storage.db import Database

log = structlog.get_logger(__name__)
ZERO = Decimal(0)
ERROR_ALERT_THRESHOLD = 3


def _norm_reason(r: str) -> str:
    r = re.sub(r"[a-z0-9]+_(idr|usdt|btc)", "<pair>", r)
    return re.sub(r"-?\d[\d.,:+\-T]*%?", "#", r)[:80]


async def _aw(x):
    """Await coroutines, pass plain values through (paper broker is sync, live executor async)."""
    return await x if inspect.isawaitable(x) else x


class AgentRunner:
    def __init__(self, settings: Settings, db: Database, market_data, mode: str = "paper",
                 notifier: Notifier | None = None, llm=None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 trade_client=None, deadman=None):
        if mode not in ("paper", "live"):
            raise ValueError(f"unsupported mode {mode!r}")
        if mode == "live" and trade_client is None:
            raise ValueError("live mode requires a trade client")
        self.s, self.db, self.md, self.mode = settings, db, market_data, mode
        self.trade_client, self.deadman = trade_client, deadman
        self.notifier = notifier or NullNotifier()
        self.now = now
        self.tz = ZoneInfo(settings.reporting.timezone)
        self.capital = Decimal(str(settings.risk.agent_capital_idr))
        self.engine = DecisionEngine(settings, db, mode, llm=llm)
        self.pf = rebuild_portfolio(db, mode, self.capital)
        if mode == "live":
            self.broker = LiveExecutor(db, self.pf, trade_client, CostModel(settings.fees),
                                       settings.risk.emergency_exit_max_slippage_pct, mode)
        else:
            self.broker = PaperBroker(db, self.pf, CostModel(settings.fees), mode)
        self.last_recon = None
        self.started_at = now()
        self.consecutive_errors = 0
        self.last_markets: dict[str, PairMarket] = {}
        self.last_features: dict[str, dict[str, TimeframeFeatures]] = {}
        self.last_notes: dict[str, str] = {}

    # ------------------------------------------------------------ status

    @property
    def status(self) -> AgentStatus:
        return AgentStatus(self.db.get_state(f"{self.mode}:status", AgentStatus.RUNNING.value))

    @property
    def pause_reason(self) -> str | None:
        return self.db.get_state(f"{self.mode}:pause_reason")

    def _set_status(self, status: AgentStatus, reason: str | None = None) -> None:
        self.db.set_state(f"{self.mode}:status", status.value)
        self.db.set_state(f"{self.mode}:pause_reason", reason)
        log.warning("status_changed", status=status.value, reason=reason)

    def local_date(self, t: datetime) -> str:
        return t.astimezone(self.tz).date().isoformat()

    def day_bounds(self, t: datetime) -> tuple[datetime, datetime]:
        local = t.astimezone(self.tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)

    # ------------------------------------------------------------ startup

    async def update_clock(self, clock_offset_ms: int | None) -> str | None:
        """Pause entries while the VPS clock is off; resume automatically once it is fixed.
        Returns a note when the status changed."""
        if clock_offset_ms is None:
            return None
        bad = abs(clock_offset_ms) > self.s.exchange.max_clock_offset_ms
        if bad and self.status == AgentStatus.RUNNING:
            self._set_status(AgentStatus.PAUSED, "clock")
            return (f"Jam VPS selisih {clock_offset_ms} ms dari server Indodax (maks "
                    f"{self.s.exchange.max_clock_offset_ms} ms) — entry dihentikan sampai jam benar. Cek chrony/NTP.")
        if not bad and self.status == AgentStatus.PAUSED and self.pause_reason == "clock":
            self._set_status(AgentStatus.RUNNING)
            return f"Jam VPS kembali normal ({clock_offset_ms} ms) — entry dibuka kembali."
        return None

    async def check_clock_job(self, public_client) -> None:
        try:
            offset = await public_client.clock_offset_ms()
            if self.trade_client is not None and offset is not None:
                self.trade_client.clock.offset_ms = offset   # keep signed timestamps inside recvWindow
            note = await self.update_clock(offset)
        except Exception as e:  # noqa: BLE001
            self.db.record_error("clock", f"clock check failed: {type(e).__name__}")
            return
        if note:
            await self.notifier.send(f"🕒 {esc(note)}")

    async def startup(self, clock_offset_ms: int | None = None, extra_notes: list[str] | None = None) -> None:
        notes = list(extra_notes or [])
        clock_note = await self.update_clock(clock_offset_ms)
        if clock_note:
            notes.append(clock_note)
        L = self.s.risk
        banner = ["⚠️ <b>MODE LIVE — order NYATA di akun Indodax</b>"] if self.mode == "live" else []
        msg = banner + [
            f"🔄 <b>Agent start</b> — mode <b>{self.mode.upper()}</b>, status <b>{self.status.value}</b>",
            f"Modal agent {rp(self.capital)} · kas {rp(self.pf.cash_idr)} · posisi {len(self.pf.positions)}",
            f"Limit: posisi ≤{L.max_position_pct}% · maks {L.max_open_positions} posisi · aset ≤{L.max_asset_exposure_pct}% · "
            f"rugi harian {L.daily_loss_limit_pct}% · kill switch DD {L.max_drawdown_pct}% · "
            f"{L.max_orders_per_hour} order/jam, {L.max_orders_per_day}/hari · cooldown SL {L.stoploss_cooldown_minutes}m",
            f"Whitelist: {', '.join(self.s.market.whitelist)}",
        ] + [f"⚠️ {esc(n)}" for n in notes]
        await self.notifier.send("\n".join(msg))

    # -------------------------------------------------------------- cycle

    async def fetch_markets(self) -> dict[str, PairMarket]:
        pairs = list(dict.fromkeys([*self.s.market.whitelist, *self.pf.positions]))
        out = {}
        for p in pairs:
            try:
                out[p] = await self.md.fetch(p)
            except Exception as e:  # noqa: BLE001
                self.db.record_error("market_data", f"{p}: {type(e).__name__}: {e}"[:300])
                log.warning("market_fetch_failed", pair=p, error=str(e)[:200])
        return out

    async def run_cycle(self) -> list[FillEvent]:
        try:
            fills = await self._cycle()
            self.consecutive_errors = 0
            return fills
        except Exception as e:  # noqa: BLE001 - the loop must survive anything
            self.consecutive_errors += 1
            self.db.record_error("cycle", f"{type(e).__name__}: {e}"[:300])
            log.exception("cycle_failed")
            if self.consecutive_errors == ERROR_ALERT_THRESHOLD:
                await self.notifier.send(f"🚨 <b>Error berulang</b> ({self.consecutive_errors}× berturut-turut): "
                                         f"{esc(type(e).__name__)}: {esc(str(e)[:200])}")
            return []

    async def _cycle(self) -> list[FillEvent]:
        now = self.now()
        today = self.local_date(now)
        reason = self.pause_reason
        if self.status == AgentStatus.PAUSED and reason and reason.startswith("daily_loss:") \
                and reason.split(":", 1)[1] != today:
            self._set_status(AgentStatus.RUNNING)
            await self.notifier.send("▶️ Hari baru — entry dibuka kembali setelah batas rugi harian kemarin.")

        markets = await self.fetch_markets()
        missing = [p for p in self.pf.positions if p not in markets]
        if missing:
            raise RuntimeError(f"no market data for open positions {missing}")
        self.last_markets = markets
        fills = await _aw(self.broker.check_resting(markets, now))
        recon_ok, free_idr = True, None
        if self.mode == "live":
            recon_ok, free_idr = await self._live_checks()
        res = await self.engine.run_cycle(markets, self.pf, now, self.status,
                                          reconciliation_ok=recon_ok, pending_buy_idr=self.broker.pending_buy_idr(),
                                          exchange_free_idr=free_idr)
        self.last_notes = res.notes
        self.last_features = {p: self.engine.features(m) for p, m in markets.items()}
        for row_id, d in res.approved:
            fills += await _aw(self.broker.execute(row_id, d, markets[d.proposal.pair], now))
        save_stops(self.db, self.mode, self.pf)   # trailing stops raised by the engine

        if res.trigger_halt and self.status != AgentStatus.HALTED:
            n = await _aw(self.broker.cancel_all(now))
            self._set_status(AgentStatus.HALTED, "max_drawdown")
            await self.notifier.send(
                f"🛑 <b>KILL SWITCH</b>: drawdown ≥ {self.s.risk.max_drawdown_pct}% dari puncak. "
                f"{n} order dibatalkan, status HALTED. Posisi tetap dipegang dengan stop-loss. "
                "Kirim /resume untuk melanjutkan.")
        elif res.trigger_daily_stop and self.status == AgentStatus.RUNNING:
            self._set_status(AgentStatus.PAUSED, f"daily_loss:{today}")
            await self.notifier.send(
                f"⚠️ <b>Batas rugi harian</b> {self.s.risk.daily_loss_limit_pct}% tercapai — "
                "tidak ada entry baru sampai besok (WIB). Stop-loss tetap aktif.")

        for f in fills:
            if f.is_stop_loss:
                await self.notifier.send(f"🔻 <b>Stop-loss</b> {esc(f.pair)}: jual {f.qty} @ {rp(f.price)} · "
                                         f"PnL {rp(f.realized_pnl)} · fee {rp(f.fee_idr)}")
            else:
                pnl = f" · PnL {rp(f.realized_pnl)}" if f.side == "sell" else ""
                await self.notifier.send(f"✅ <b>Order terisi</b> ({self.mode}) {esc(f.pair)} {f.side.upper()} "
                                         f"{f.qty} @ {rp(f.price)} · fee {rp(f.fee_idr)}{pnl}")
        self._book_day(now, markets)
        return fills

    async def _auto_pause(self, reason: str, bad: bool, bad_msg: str, ok_msg: str) -> None:
        """Pause entries while a condition is bad; lift only a pause we set for that reason."""
        if bad and self.status == AgentStatus.RUNNING:
            self._set_status(AgentStatus.PAUSED, reason)
            await self.notifier.send(bad_msg)
        elif not bad and self.status == AgentStatus.PAUSED and self.pause_reason == reason:
            self._set_status(AgentStatus.RUNNING)
            await self.notifier.send(ok_msg)

    async def _live_checks(self) -> tuple[bool, Decimal | None]:
        pairs = list(dict.fromkeys([*self.s.market.whitelist, *self.pf.positions]))
        rec = await reconcile(self.trade_client, self.db, self.mode, self.pf, pairs, self.broker.is_ours)
        self.last_recon = rec
        if rec.issues:
            self.db.record_error("reconcile", "; ".join(rec.issues)[:300])
        await self._auto_pause(
            "reconcile", not rec.ok,
            "🚨 <b>Rekonsiliasi gagal</b> — semua order dihentikan:\n" + esc("\n".join(rec.issues[:5])),
            "✅ Rekonsiliasi kembali cocok — agent lanjut.")
        if self.deadman is not None:
            await self._auto_pause(
                "deadman", not self.deadman.healthy,
                "⚠️ Deadman Switch tidak sehat — entry baru dihentikan.",
                "✅ Deadman Switch sehat — entry dibuka kembali.")
        return rec.ok, rec.exchange_free_idr

    def _marks(self, markets: dict[str, PairMarket]) -> dict[str, Decimal]:
        return {p: (markets[p].orderbook.best_bid or markets[p].ticker.last) for p in self.pf.positions
                if p in markets}

    def _book_day(self, now: datetime, markets: dict[str, PairMarket]) -> None:
        marks = self._marks(markets)
        if len(marks) != len(self.pf.positions):
            return
        start, end = self.day_bounds(now)
        fills = self.db.fills_between(start, end, self.mode)
        realized = sum((Decimal(f["realized_pnl"] or 0) for f in fills), ZERO)
        fees = sum((Decimal(f["fee_idr"]) for f in fills), ZERO)
        equity = self.pf.equity(marks)
        peak = Decimal(self.db.get_state(f"{self.mode}:peak_equity", "0"))
        date = self.local_date(now)
        row = self.db.get_daily_pnl(date, self.mode)
        self.db.upsert_daily_pnl(date, self.mode, start_equity=Decimal(row["start_equity"]) if row else equity,
                                 end_equity=equity, realized=realized, unrealized=self.pf.unrealized_pnl(marks),
                                 fees=fees, peak_equity=max(peak, equity))

    # ----------------------------------------------------------- commands

    async def pause(self) -> str:
        if self.status == AgentStatus.HALTED:
            return "Agent sudah HALTED. Gunakan /resume untuk melanjutkan."
        self._set_status(AgentStatus.PAUSED, "manual")
        return "⏸️ Agent PAUSED — tidak ada entry baru. Stop-loss tetap aktif. /resume untuk melanjutkan."

    async def resume(self) -> str:
        st = self.status
        if st == AgentStatus.RUNNING:
            return "Agent sudah RUNNING."
        note = ""
        if st == AgentStatus.HALTED and self.last_markets:
            marks = self._marks(self.last_markets)
            if len(marks) == len(self.pf.positions):
                eq = self.pf.equity(marks)
                self.db.set_state(f"{self.mode}:peak_equity", str(eq))
                note = f"\nPuncak equity untuk kill switch di-reset ke {rp(eq)}."
        self._set_status(AgentStatus.RUNNING)
        return f"▶️ Agent RUNNING (sebelumnya {st.value}).{note}"

    async def kill(self) -> str:
        n = await _aw(self.broker.cancel_all(self.now()))
        self._set_status(AgentStatus.HALTED, "manual_kill")
        await self.notifier.send(f"🛑 <b>/kill</b>: {n} order dibatalkan, status HALTED. "
                                 "Posisi tetap dipegang dengan stop-loss-nya. /resume untuk melanjutkan.")
        return f"🛑 HALTED. {n} order dibatalkan. Posisi tetap dipegang dengan stop-loss."

    # ------------------------------------------------------------- views

    def status_text(self) -> str:
        marks = self._marks(self.last_markets)
        eq = self.pf.equity(marks) if len(marks) == len(self.pf.positions) else None
        peak = Decimal(self.db.get_state(f"{self.mode}:peak_equity", "0"))
        dd = f"{float((peak - eq) / peak * 100):.2f}%" if eq is not None and peak > 0 else "—"
        reason = f" ({esc(self.pause_reason)})" if self.pause_reason else ""
        up = self.now() - self.started_at
        return (f"<b>Status</b> {self.status.value}{reason} · mode {self.mode.upper()}\n"
                f"Equity {rp(eq)} · kas {rp(self.pf.cash_idr)} · drawdown {dd}\n"
                f"Posisi {len(self.pf.positions)} · order resting {len(self.broker.resting_orders())}\n"
                f"Uptime {int(up.total_seconds() // 3600)} jam · paper days tersimpan {self.db.paper_days_recorded()}")

    def positions_text(self) -> str:
        if not self.pf.positions:
            return "Tidak ada posisi terbuka."
        marks = self._marks(self.last_markets)
        lines = ["<b>Posisi terbuka</b>"]
        for k, p in self.pf.positions.items():
            m = marks.get(k)
            un = rp((m - p.avg_cost) * p.qty) if m else "—"
            lines.append(f"{esc(k)} {p.qty} @ {rp(p.avg_cost)} · harga {rp(m)} · unrealized {un} · "
                         f"SL {rp(p.stop_loss)} · TP {rp(p.take_profit) if p.take_profit else 'trailing'}")
        return "\n".join(lines)

    def outlook_text(self) -> str:
        parts = []
        for pair in self.s.market.whitelist:
            f = (self.last_features.get(pair) or {}).get(self.s.strategy.timeframes[0])
            if f is None or f.close != f.close:
                continue
            name = pair.split("_")[0].upper()
            trend = "di atas" if f.close > f.ema_trend else "di bawah"
            regime = {Regime.TRENDING_UP: "tren naik", Regime.TRENDING_DOWN: "tren turun",
                      Regime.RANGING: "sideways", Regime.HIGH_VOLATILITY: "volatil"}.get(f.regime, "tidak jelas")
            if pair in self.pf.positions:
                parts.append(f"{name}: {regime}, posisi dipegang (stop {rp(self.pf.positions[pair].stop_loss)}).")
            elif f.close > f.ema_trend and f.donchian_high == f.donchian_high:
                gap = (f.donchian_high / f.close - 1) * 100
                parts.append(f"{name}: {regime}, {trend} EMA{self.s.strategy.trend_ema}; "
                             f"sinyal beli jika close harian > {rp(f.donchian_high)} (+{gap:.1f}%).")
            else:
                parts.append(f"{name}: {regime}, {trend} EMA{self.s.strategy.trend_ema} — tidak ada entry.")
        return " ".join(parts[:3]) if parts else "Data belum cukup untuk pandangan pasar."

    def report_data(self, title: str = "Laporan harian") -> ReportData:
        now = self.now()
        start, end = self.day_bounds(now)
        marks = self._marks(self.last_markets)
        full_marks = len(marks) == len(self.pf.positions)
        equity = self.pf.equity(marks) if full_marks else self.pf.cash_idr
        row = self.db.get_daily_pnl(self.local_date(now), self.mode)
        day_start = Decimal(row["start_equity"]) if row else equity
        peak = max(Decimal(self.db.get_state(f"{self.mode}:peak_equity", "0")), equity)
        fills = self.db.fills_between(start, end, self.mode)
        reasons = {}
        for o in self.db._conn.execute(
                "SELECT o.client_order_id, d.setup, d.proposal_reason FROM orders o "
                "LEFT JOIN decisions d ON d.id = o.decision_id WHERE o.mode = ?", (self.mode,)):
            reasons[o["client_order_id"]] = o["setup"] or (o["proposal_reason"] or "")[:40]
        fill_lines = [FillLine(datetime.fromisoformat(f["ts"]).astimezone(self.tz), f["pair"], f["side"],
                               Decimal(f["price"]), Decimal(f["qty"]), Decimal(f["fee_idr"]),
                               Decimal(f["realized_pnl"]) if f["realized_pnl"] is not None else None,
                               reasons.get(f["client_order_id"], "")) for f in fills]
        decisions = self.db.decisions_between(start, end)
        vetoes = [d for d in decisions if d["verdict"] == Verdict.VETO.value]
        top = Counter(_norm_reason(json.loads(d["reasons_json"])[0]) for d in vetoes
                      if json.loads(d["reasons_json"])).most_common(3)
        errors = [(datetime.fromisoformat(e["ts"]).astimezone(self.tz), e["component"], e["message"])
                  for e in self.db.errors_between(start, end)]
        positions = [PositionLine(k, p.qty, p.avg_cost, marks.get(k, p.avg_cost), p.stop_loss, p.take_profit)
                     for k, p in self.pf.positions.items()]
        notes = [] if full_marks else ["Harga pasar belum tersedia untuk semua posisi; equity dihitung dari kas."]
        return ReportData(
            mode=self.mode, status=self.status.value, pause_reason=self.pause_reason,
            uptime=now - self.started_at, now_local=now.astimezone(self.tz),
            day_start_equity=day_start, equity=equity, peak_equity=peak,
            realized_today=sum((f.pnl or ZERO for f in fill_lines if f.side == "sell"), ZERO),
            fees_today=sum((f.fee for f in fill_lines), ZERO), fills=fill_lines, positions=positions,
            proposals=len(decisions), approved=len(decisions) - len(vetoes), vetoed=len(vetoes),
            top_veto_reasons=top, errors=errors, outlook=self.outlook_text(), title=title, notes=notes,
        )

    def report_text(self) -> str:
        return build_daily_report(self.report_data("Laporan (on-demand)"))

    async def send_daily_report(self) -> None:
        await self.notifier.send(build_daily_report(self.report_data()))

