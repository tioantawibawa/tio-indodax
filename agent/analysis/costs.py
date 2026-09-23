"""Trading cost model: fees + tax + clearing + spread + slippage.

Assumption (conservative): entry is a maker limit order, exit may be a taker
(stop-loss or take-profit crossing the book), so the round trip pays maker +
taker fee, tax and clearing on both legs, the spread once and slippage once.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from agent.config import FeeSettings
from agent.exchange.models import PairInfo


@dataclass(frozen=True)
class CostBreakdown:
    entry_fee_pct: Decimal
    exit_fee_pct: Decimal
    tax_clearing_pct: Decimal
    spread_pct: Decimal
    slippage_pct: Decimal

    @property
    def total_pct(self) -> Decimal:
        return (self.entry_fee_pct + self.exit_fee_pct + self.tax_clearing_pct
                + self.spread_pct + self.slippage_pct)


class CostModel:
    def __init__(self, fees: FeeSettings):
        self.fees = fees

    def _maker(self, info: PairInfo | None) -> Decimal:
        if info is not None and info.maker_fee_pct is not None:
            return info.maker_fee_pct
        return Decimal(str(self.fees.maker_pct))

    def _taker(self, info: PairInfo | None) -> Decimal:
        if info is not None and info.taker_fee_pct is not None:
            return info.taker_fee_pct
        return Decimal(str(self.fees.taker_pct))

    def leg_fee_pct(self, info: PairInfo | None, maker: bool) -> Decimal:
        """Fee + tax + clearing for one leg, in percent of notional."""
        base = self._maker(info) if maker else self._taker(info)
        return base + Decimal(str(self.fees.tax_pct)) + Decimal(str(self.fees.clearing_pct))

    def round_trip(self, info: PairInfo | None, spread_pct: Decimal, slippage_pct: Decimal,
                   entry_maker: bool = True, exit_maker: bool = False) -> CostBreakdown:
        tax_clear = 2 * (Decimal(str(self.fees.tax_pct)) + Decimal(str(self.fees.clearing_pct)))
        return CostBreakdown(
            entry_fee_pct=self._maker(info) if entry_maker else self._taker(info),
            exit_fee_pct=self._maker(info) if exit_maker else self._taker(info),
            tax_clearing_pct=tax_clear,
            spread_pct=max(spread_pct, Decimal(0)),
            slippage_pct=max(slippage_pct, Decimal(0)),
        )

    def fee_idr(self, info: PairInfo | None, notional_idr: Decimal, maker: bool) -> Decimal:
        return notional_idr * self.leg_fee_pct(info, maker) / 100
