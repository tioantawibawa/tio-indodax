"""Live order executor (legacy /tapi). Same interface as PaperBroker.

Double-order protection:
1. client_order_id = "<prefix><salt>-<decision_id>" — one id per decision; the
   salt is random per database, so a wiped DB can never reuse an old id.
2. The order row is written as PENDING_SUBMIT *before* the request is sent.
3. ``trade`` is never auto-retried. If the outcome is unknown (timeout,
   connection reset) the order is looked up by client_order_id; if the
   exchange has no such order after ``pending_grace_s`` it is marked REJECTED.
   A decision that already has an order row is never submitted again.
4. Fills are recorded from the *cumulative* executed quantity, so re-reading
   an order can never double count (trade_id = "<coid>:<cumulative qty>").

Emergency ("market") exits are marketable limit sells at
best_bid × (1 − emergency_exit_max_slippage_pct): immediate, but bounded.
Only orders whose client_order_id carries our prefix are ever cancelled.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal

import structlog

from agent.analysis.costs import CostModel
from agent.backtest.paper_broker import FillEvent
from agent.data.market_data import PairMarket
from agent.exchange.errors import IndodaxAPIError, IndodaxError
from agent.exchange.models import PairInfo
from agent.exchange.trade_client import LiveTradeClient, OrderState
from agent.portfolio.persistence import save_stops
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import RiskDecision
from agent.storage.db import Database

log = structlog.get_logger(__name__)
ZERO = Decimal(0)
PREFIX = "ag"


class LiveExecutor:
    def __init__(self, db: Database, portfolio: Portfolio, client: LiveTradeClient, costs: CostModel,
                 emergency_slippage_pct: float, mode: str = "live", entry_ttl_s: int = 120,
                 pending_grace_s: int = 300):
        self.db, self.pf, self.client, self.costs, self.mode = db, portfolio, client, costs, mode
        self.emergency_slippage = Decimal(str(emergency_slippage_pct))
        self.entry_ttl_s, self.pending_grace_s = entry_ttl_s, pending_grace_s
        salt = db.get_state("executor:salt")
        if not salt:
            salt = secrets.token_hex(3)
            db.set_state("executor:salt", salt)
        self.id_prefix = f"{PREFIX}{salt}-"
        self.infos: dict[str, PairInfo] = {}

    def coid(self, decision_id: int) -> str:
        return f"{self.id_prefix}{decision_id}"

    def is_ours(self, client_order_id: str) -> bool:
        return client_order_id.startswith(self.id_prefix)

    # ------------------------------------------------------------ helpers

    def _fill(self, coid: str, pair: str, side: str, qty: Decimal, price: Decimal, fee: Decimal | None,
              maker: bool, cumulative: Decimal, now: datetime, reason: str, stop_loss: Decimal | None,
              is_stop: bool) -> FillEvent | None:
        info = self.infos.get(pair)
        if side == "sell":
            held = self.pf.positions.get(pair)
            qty = min(qty, held.qty if held else ZERO)
        if qty <= 0:
            return None
        if fee is None:  # legacy getOrder has no fee field: conservative estimate (reconciled via balances)
            fee = self.costs.fee_idr(info, qty * price, maker)
        trade_id = f"{coid}:{cumulative}"
        if self.db._conn.execute("SELECT 1 FROM fills WHERE trade_id = ?", (trade_id,)).fetchone():
            return None  # already recorded
        if side == "buy" and qty * price + fee > self.pf.cash_idr:
            log.warning("live_fill_exceeds_ledger_cash", pair=pair, cost=str(qty * price + fee),
                        cash=str(self.pf.cash_idr))
        r = self.pf.apply_fill(pair, side, qty, price, fee, now, stop_loss, None, reason, strict=False)
        self.db.record_fill(trade_id=trade_id, client_order_id=coid, ts=now, mode=self.mode, pair=pair,
                            side=side, price=price, qty=qty, fee_idr=fee, realized_pnl=r.realized_pnl)
        if is_stop:
            self.db.record_stoploss(pair, self.mode, now)
        save_stops(self.db, self.mode, self.pf)
        log.info("live_fill", pair=pair, side=side, qty=str(qty), price=str(price), fee=str(fee))
        return FillEvent(now, pair, side, qty, price, fee, r.realized_pnl, reason, is_stop, coid)

    def _decision_meta(self, decision_id) -> tuple[Decimal | None, str, bool]:
        row = self.db._conn.execute("SELECT stop_loss, setup, proposal_reason FROM decisions WHERE id = ?",
                                    (decision_id,)).fetchone()
        if row is None:
            return None, "", False
        stop = Decimal(row["stop_loss"]) if row["stop_loss"] else None
        return stop, row["setup"] or (row["proposal_reason"] or "")[:40], row["setup"] == "stop_loss"

    async def _apply_state(self, o, st: OrderState, now: datetime) -> list[FillEvent]:
        """Record fills up to ``st.filled_qty`` and update the order row."""
        events: list[FillEvent] = []
        recorded = Decimal(o["filled_qty"] or "0")
        delta = st.filled_qty - recorded
        if delta > 0:
            stop, reason, is_stop = self._decision_meta(o["decision_id"])
            ev = self._fill(o["client_order_id"], o["pair"], o["side"], delta, st.price or Decimal(o["price"]),
                            None, maker=not o["is_emergency"], cumulative=st.filled_qty, now=now, reason=reason,
                            stop_loss=stop if o["side"] == "buy" else None, is_stop=is_stop)
            if ev:
                events.append(ev)
        status = {"filled": "FILLED", "cancelled": "CANCELLED"}.get(st.status)
        if status is None:
            status = "PARTIALLY_FILLED" if st.filled_qty > 0 else "NEW"
        self.db.update_order(o["client_order_id"], status=status, exchange_order_id=st.order_id or None,
                             filled_qty=max(st.filled_qty, recorded), ts=now)
        return events

    # ---------------------------------------------------------- execution

    async def execute(self, decision_id: int, d: RiskDecision, m: PairMarket, now: datetime) -> list[FillEvent]:
        if not d.approved:
            return []
        self.infos[m.pair] = m.info
        coid = self.coid(decision_id)
        if self.db.get_order(coid) is not None:
            log.warning("live_duplicate_ignored", client_order_id=coid)
            return []
        p = d.proposal
        events: list[FillEvent] = []
        if p.side == "sell":
            events += await self.cancel_pair(p.pair, now)       # cancel/replace older agent orders
            held = self.pf.positions.get(p.pair)
            qty = m.info.round_qty(min(d.qty, held.qty if held else ZERO))
            if p.order_type == "market":
                if m.orderbook.best_bid is None:
                    return events
                price = m.info.round_price(m.orderbook.best_bid * (1 - self.emergency_slippage / 100), "buy")
            else:
                price = d.price
        else:
            qty, price = d.qty, d.price
        if qty <= 0:
            return events
        self.db.record_order(client_order_id=coid, decision_id=decision_id, mode=self.mode, pair=p.pair,
                             side=p.side, order_type="limit", price=price, qty=qty, status="PENDING_SUBMIT",
                             is_emergency=p.is_emergency_exit, ts=now)
        try:
            ack = await self.client.place_limit(p.pair, p.side, price, qty, coid)
        except IndodaxAPIError as e:
            if "already exist" in str(e).lower():
                log.warning("live_coid_exists_resolving", client_order_id=coid)
                return events + await self._resolve(coid, now)
            self.db.update_order(coid, status="REJECTED", ts=now)
            self.db.record_error("executor", f"order rejected {coid}: {e}"[:300])
            return events
        except IndodaxError as e:  # network/format: outcome unknown -> resolve by client_order_id
            log.warning("live_submit_unknown_outcome", client_order_id=coid, error=type(e).__name__)
            return events + await self._resolve(coid, now)
        self.db.update_order(coid, status="NEW", exchange_order_id=ack.order_id, ts=now)
        if ack.filled_qty > 0:
            stop = p.stop_loss if p.side == "buy" else None
            fill_price = (ack.filled_quote / ack.filled_qty) if ack.filled_quote > 0 else price
            ev = self._fill(coid, p.pair, p.side, ack.filled_qty, fill_price, ack.fee_idr, maker=False,
                            cumulative=ack.filled_qty, now=now, reason=p.setup or p.reason[:40],
                            stop_loss=stop, is_stop=p.setup == "stop_loss")
            if ev:
                events.append(ev)
            self.db.update_order(coid, filled_qty=ack.filled_qty, ts=now)
        return events + await self._resolve(coid, now)

    async def _resolve(self, coid: str, now: datetime) -> list[FillEvent]:
        o = self.db.get_order(coid)
        try:
            st = await self.client.get_order_by_coid(coid, o["pair"])
        except IndodaxError as e:
            log.warning("live_order_lookup_failed", client_order_id=coid, error=type(e).__name__)
            return []  # retried by check_resting next cycle
        if st is None:
            age = (now - datetime.fromisoformat(o["ts_created"])).total_seconds()
            if o["status"] == "PENDING_SUBMIT" and age >= self.pending_grace_s:
                self.db.update_order(coid, status="REJECTED", ts=now)
                log.warning("live_order_never_reached_exchange", client_order_id=coid)
            return []
        return await self._apply_state(o, st, now)

    async def check_resting(self, markets: dict[str, PairMarket], now: datetime) -> list[FillEvent]:
        """Per-cycle sync of every open agent order; expires stale entries."""
        for p, m in markets.items():
            self.infos[p] = m.info
        events: list[FillEvent] = []
        for o in self.resting_orders():
            events += await self._resolve(o["client_order_id"], now)
            o = self.db.get_order(o["client_order_id"])
            if o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                continue
            age = (now - datetime.fromisoformat(o["ts_created"])).total_seconds()
            if o["side"] == "buy" and age >= self.entry_ttl_s:
                events += await self._cancel_row(o, now)
        return events

    async def _cancel_row(self, o, now: datetime) -> list[FillEvent]:
        try:
            await self.client.cancel(o["pair"], o["exchange_order_id"], o["side"])
        except IndodaxError as e:
            self.db.record_error("executor", f"cancel failed {o['client_order_id']}: {e}"[:300])
            return []
        return await self._resolve(o["client_order_id"], now)   # records fills that raced the cancel

    async def cancel_pair(self, pair: str, now: datetime) -> list[FillEvent]:
        events: list[FillEvent] = []
        for o in self.resting_orders():
            if o["pair"] == pair and o["exchange_order_id"]:
                events += await self._cancel_row(o, now)
        return events

    async def cancel_all(self, now: datetime) -> int:
        n = 0
        for o in self.resting_orders():
            if o["exchange_order_id"]:
                await self._cancel_row(o, now)
                n += 1
        return n

    def resting_orders(self) -> list:
        return [o for o in self.db.open_orders(self.mode) if self.is_ours(o["client_order_id"])]

    def pending_buy_idr(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for o in self.resting_orders():
            if o["side"] == "buy":
                remaining = Decimal(o["qty"]) - Decimal(o["filled_qty"] or "0")
                out[o["pair"]] = out.get(o["pair"], ZERO) + Decimal(o["price"]) * remaining
        return out
