"""Paper broker: simulates execution against the LIVE orderbook, never calls
an order endpoint.

- Marketable orders (buy limit >= best ask, sell limit <= best bid, emergency
  market sells) walk the real orderbook levels, pay the taker fee and are
  treated as immediate-or-cancel: any part the book cannot fill within the
  limit is cancelled.
- Passive limit orders rest and fill (maker fee) once the opposite best price
  reaches the limit, or are cancelled after ``resting_ttl_min``.
- Every order and fill is written to the DB with a deterministic
  client_order_id (``paper-<decision_id>``), so a restart never executes the
  same decision twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import structlog

from agent.analysis.costs import CostModel
from agent.data.market_data import PairMarket
from agent.portfolio.persistence import save_stops
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import RiskDecision
from agent.storage.db import Database

log = structlog.get_logger(__name__)
ZERO = Decimal(0)


@dataclass(frozen=True)
class FillEvent:
    ts: datetime
    pair: str
    side: str
    qty: Decimal
    price: Decimal          # VWAP of the fill
    fee_idr: Decimal
    realized_pnl: Decimal
    reason: str
    is_stop_loss: bool
    client_order_id: str


def _walk(levels, qty: Decimal, limit: Decimal | None, side: str) -> tuple[Decimal, Decimal]:
    """(filled_qty, notional) taking levels in order while within the limit."""
    filled = notional = ZERO
    for price, avail in levels:
        if limit is not None and ((side == "buy" and price > limit) or (side == "sell" and price < limit)):
            break
        take = min(avail, qty - filled)
        filled += take
        notional += take * price
        if filled >= qty:
            break
    return filled, notional


class PaperBroker:
    def __init__(self, db: Database, portfolio: Portfolio, costs: CostModel, mode: str = "paper",
                 resting_ttl_min: int = 30):
        self.db, self.pf, self.costs, self.mode = db, portfolio, costs, mode
        self.ttl = timedelta(minutes=resting_ttl_min)

    @staticmethod
    def coid(decision_id: int) -> str:
        return f"paper-{decision_id}"

    # ------------------------------------------------------------ helpers

    def _fill(self, coid: str, m: PairMarket, side: str, qty: Decimal, notional: Decimal, maker: bool,
              now: datetime, reason: str, stop_loss: Decimal | None, is_stop: bool) -> FillEvent:
        info = m.info
        qty = info.round_qty(qty)
        vwap = notional / qty if qty else ZERO
        fee = self.costs.fee_idr(info, qty * vwap, maker)
        r = self.pf.apply_fill(m.pair, side, qty, vwap, fee, now, stop_loss, None, reason)
        self.db.record_fill(trade_id=f"{coid}-f", client_order_id=coid, ts=now, mode=self.mode, pair=m.pair,
                            side=side, price=vwap, qty=qty, fee_idr=fee, realized_pnl=r.realized_pnl)
        if is_stop:
            self.db.record_stoploss(m.pair, self.mode, now)
        save_stops(self.db, self.mode, self.pf)
        log.info("paper_fill", pair=m.pair, side=side, qty=str(qty), price=str(vwap), fee=str(fee))
        return FillEvent(now, m.pair, side, qty, vwap, fee, r.realized_pnl, reason, is_stop, coid)

    # ---------------------------------------------------------- execution

    def execute(self, decision_id: int, d: RiskDecision, m: PairMarket, now: datetime) -> list[FillEvent]:
        if not d.approved:
            return []
        coid = self.coid(decision_id)
        if self.db.get_order(coid) is not None:
            log.warning("paper_duplicate_ignored", client_order_id=coid)
            return []
        p = d.proposal
        book = m.orderbook
        is_market = p.order_type == "market"
        self.db.record_order(client_order_id=coid, decision_id=decision_id, mode=self.mode, pair=p.pair,
                             side=p.side, order_type=p.order_type, price=d.price, qty=d.qty,
                             status="NEW", is_emergency=p.is_emergency_exit, ts=now)
        if p.side == "buy":
            marketable = book.best_ask is not None and d.price >= book.best_ask
            levels, limit = book.asks, d.price
        else:
            marketable = is_market or (book.best_bid is not None and d.price <= book.best_bid)
            levels, limit = book.bids, (None if is_market else d.price)
        if not marketable:
            return []   # rests; see check_resting
        qty = d.qty
        if p.side == "sell":
            held = self.pf.positions.get(p.pair)
            qty = min(qty, held.qty if held else ZERO)
        available, _ = _walk(levels, qty, limit, p.side)
        filled = m.info.round_qty(available)
        if filled <= 0:
            self.db.update_order(coid, status="CANCELLED", ts=now)
            return []
        _, notional = _walk(levels, filled, limit, p.side)
        ev = self._fill(coid, m, p.side, filled, notional, maker=False, now=now,
                        reason=p.setup or p.reason[:60], stop_loss=p.stop_loss, is_stop=p.setup == "stop_loss")
        status = "FILLED" if filled >= qty else "CANCELLED"  # IOC: remainder cancelled
        self.db.update_order(coid, status=status, filled_qty=filled, ts=now)
        return [ev]

    def resting_orders(self) -> list:
        return [o for o in self.db.open_orders(self.mode) if o["client_order_id"].startswith("paper-")]

    def check_resting(self, markets: dict[str, PairMarket], now: datetime) -> list[FillEvent]:
        events: list[FillEvent] = []
        for o in self.resting_orders():
            coid, pair = o["client_order_id"], o["pair"]
            created = datetime.fromisoformat(o["ts_created"])
            m = markets.get(pair)
            price, qty = Decimal(o["price"]), Decimal(o["qty"])
            touched = m is not None and (
                (o["side"] == "buy" and m.orderbook.best_ask is not None and m.orderbook.best_ask <= price)
                or (o["side"] == "sell" and m.orderbook.best_bid is not None and m.orderbook.best_bid >= price))
            if touched:
                stop = None
                if o["side"] == "buy" and o["decision_id"] is not None:
                    row = self.db._conn.execute("SELECT stop_loss FROM decisions WHERE id = ?",
                                                (o["decision_id"],)).fetchone()
                    stop = Decimal(row["stop_loss"]) if row and row["stop_loss"] else None
                if o["side"] == "sell":
                    held = self.pf.positions.get(pair)
                    qty = min(qty, held.qty if held else ZERO)
                    if qty <= 0:
                        self.db.update_order(coid, status="CANCELLED", ts=now)
                        continue
                events.append(self._fill(coid, m, o["side"], qty, qty * price, maker=True, now=now,
                                         reason="resting limit", stop_loss=stop, is_stop=False))
                self.db.update_order(coid, status="FILLED", filled_qty=qty, ts=now)
            elif now - created >= self.ttl:
                self.db.update_order(coid, status="CANCELLED", ts=now)
                log.info("paper_order_expired", client_order_id=coid)
        return events

    def cancel_all(self, now: datetime) -> int:
        n = 0
        for o in self.resting_orders():
            self.db.update_order(o["client_order_id"], status="CANCELLED", ts=now)
            n += 1
        return n

    def pending_buy_idr(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for o in self.resting_orders():
            if o["side"] == "buy":
                out[o["pair"]] = out.get(o["pair"], ZERO) + Decimal(o["price"]) * Decimal(o["qty"])
        return out
