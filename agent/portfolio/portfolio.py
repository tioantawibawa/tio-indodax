"""Agent ledger: cash, positions, average cost, realized/unrealized PnL.

The ledger tracks only the agent's own allocation (``agent_capital_idr``);
funds in the Indodax account outside that allocation are never counted and
therefore never used. Spot only: positions are long, quantities >= 0.
Fees are assumed to be charged in IDR (Indodax ``commissionAsset: idr``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from agent.exchange.pairs import base_asset

ZERO = Decimal(0)


@dataclass(frozen=True)
class Position:
    pair: str
    qty: Decimal
    avg_cost: Decimal          # IDR per unit, including buy fees
    stop_loss: Decimal | None
    take_profit: Decimal | None
    opened_at: datetime
    reason: str = ""

    def value(self, price: Decimal) -> Decimal:
        return self.qty * price

    def unrealized(self, price: Decimal) -> Decimal:
        return (price - self.avg_cost) * self.qty


@dataclass(frozen=True)
class FillResult:
    realized_pnl: Decimal
    position: Position | None


class Portfolio:
    def __init__(self, cash_idr: Decimal):
        if cash_idr < 0:
            raise ValueError("cash cannot be negative")
        self.cash_idr = cash_idr
        self.positions: dict[str, Position] = {}
        self.realized_pnl = ZERO
        self.fees_paid = ZERO

    # ---------------------------------------------------------------- fills

    def apply_fill(
        self,
        pair: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        fee_idr: Decimal,
        ts: datetime,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        reason: str = "",
    ) -> FillResult:
        if qty <= 0 or price <= 0 or fee_idr < 0:
            raise ValueError("qty and price must be positive, fee non-negative")
        self.fees_paid += fee_idr
        pos = self.positions.get(pair)
        if side == "buy":
            cost = qty * price + fee_idr
            if cost > self.cash_idr:
                raise ValueError(f"buy of {cost} IDR exceeds agent cash {self.cash_idr}")
            self.cash_idr -= cost
            if pos is None:
                new = Position(pair, qty, cost / qty, stop_loss, take_profit, ts, reason)
            else:
                total_qty = pos.qty + qty
                new = replace(
                    pos,
                    qty=total_qty,
                    avg_cost=(pos.avg_cost * pos.qty + cost) / total_qty,
                    stop_loss=stop_loss if stop_loss is not None else pos.stop_loss,
                    take_profit=take_profit if take_profit is not None else pos.take_profit,
                )
            self.positions[pair] = new
            return FillResult(ZERO, new)
        if side == "sell":
            if pos is None or qty > pos.qty:
                raise ValueError(f"cannot sell {qty} {pair}: position is {pos.qty if pos else 0} (no shorting)")
            proceeds = qty * price - fee_idr
            realized = proceeds - pos.avg_cost * qty
            self.cash_idr += proceeds
            self.realized_pnl += realized
            remaining = pos.qty - qty
            if remaining == 0:
                del self.positions[pair]
                return FillResult(realized, None)
            new = replace(pos, qty=remaining)
            self.positions[pair] = new
            return FillResult(realized, new)
        raise ValueError(f"unknown side {side!r}")

    def raise_stop(self, pair: str, new_stop: Decimal) -> bool:
        """Move the stop-loss up. A stop is never loosened: lower values are ignored."""
        pos = self.positions[pair]
        if pos.stop_loss is not None and new_stop <= pos.stop_loss:
            return False
        self.positions[pair] = replace(pos, stop_loss=new_stop)
        return True

    # ------------------------------------------------------------ valuation

    def positions_value(self, prices: Mapping[str, Decimal]) -> Decimal:
        return sum((p.value(prices[k]) for k, p in self.positions.items()), ZERO)

    def equity(self, prices: Mapping[str, Decimal]) -> Decimal:
        """Cash + positions marked at ``prices`` (use best bid for a conservative mark)."""
        return self.cash_idr + self.positions_value(prices)

    def unrealized_pnl(self, prices: Mapping[str, Decimal]) -> Decimal:
        return sum((p.unrealized(prices[k]) for k, p in self.positions.items()), ZERO)

    def exposure_by_asset(self, prices: Mapping[str, Decimal]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for k, p in self.positions.items():
            a = base_asset(k)
            out[a] = out.get(a, ZERO) + p.value(prices[k])
        return out
