"""Strategy ``trend_follow``: turns analysed market data into TradeProposals.

Long-only spot, daily candles. Design pre-registered in
docs/backtest_phase3.md before it was tested:

- Entry: daily close > highest high of the prior ``breakout_bars`` days
  (Donchian breakout) AND close > EMA(``trend_ema``). Marketable limit at the
  best ask right after the daily close.
- Stop: Chandelier exit, highest high(``chandelier_bars``) − k × ATR. The
  initial stop is the same formula; it trails upward only (never loosened).
- No fixed take-profit: positions run until the trailing stop is hit.
  ``target`` (entry + ``target_atr_mult`` × ATR) exists only so the risk
  manager can apply its "expected move ≥ 2 × costs" rule.

The strategy sizes by risk (``risk_per_trade_pct`` of capital lost if the SL
hits) but does NOT enforce limits — cost, liquidity and every hard limit are
checked by the risk manager, which can resize or veto.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from agent.analysis.signals import TimeframeFeatures
from agent.config import StrategySettings
from agent.exchange.models import OrderBook, PairInfo
from agent.portfolio.portfolio import Position

Intent = Literal["entry", "exit"]
OrderType = Literal["limit", "market"]


@dataclass(frozen=True)
class TradeProposal:
    pair: str
    side: Literal["buy", "sell"]
    order_type: OrderType
    price: Decimal              # limit price; for market orders the reference price
    qty: Decimal
    intent: Intent
    reason: str
    confidence: float           # 0..1
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None   # fixed exit level, if the strategy uses one
    target: Decimal | None = None        # expected-move reference for the cost check
    is_emergency_exit: bool = False
    setup: str = ""
    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty


def _d(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def _finite(*xs: float) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in xs)


class Strategy:
    def __init__(self, params: StrategySettings, capital_idr: Decimal):
        self.p = params
        self.capital_idr = capital_idr
        self.tf = params.timeframes[0]

    # ------------------------------------------------------------- entries

    def propose_entry(
        self,
        pair: str,
        info: PairInfo,
        book: OrderBook,
        features: dict[str, TimeframeFeatures],
    ) -> tuple[TradeProposal | None, str]:
        """Return (proposal, note). ``note`` explains why nothing was proposed."""
        f = features.get(self.tf)
        if f is None:
            return None, "missing timeframe data"
        if book.best_bid is None or book.best_ask is None:
            return None, "empty orderbook"
        if not _finite(f.close, f.donchian_high, f.ema_trend, f.atr, f.chandelier_stop) or f.atr <= 0:
            return None, "indicators not ready"
        if f.close <= f.ema_trend:
            return None, f"close {f.close:,.0f} <= EMA{self.p.trend_ema} {f.ema_trend:,.0f}"
        if f.close <= f.donchian_high:
            return None, f"no breakout: close {f.close:,.0f} <= {self.p.breakout_bars}d high {f.donchian_high:,.0f}"

        entry = info.round_price(book.best_ask, "sell")   # marketable limit at the ask
        atr = _d(f.atr)
        sl = info.round_price(_d(f.chandelier_stop), "buy")
        if sl >= entry:  # breakout candle so large that the chandelier is above price
            sl = info.round_price(entry - _d(self.p.chandelier_atr_mult) * atr, "buy")
        if sl <= 0 or sl >= entry:
            return None, "invalid stop-loss level"
        target = info.round_price(entry + _d(self.p.target_atr_mult) * atr, "sell")
        conf = 0.6
        if conf < self.p.min_confidence:
            return None, f"confidence {conf} < {self.p.min_confidence}"

        risk_idr = self.capital_idr * _d(self.p.risk_per_trade_pct) / 100
        qty = info.round_qty(risk_idr / (entry - sl))
        if qty <= 0:
            return None, "position size rounds to zero"
        reason = (f"Breakout: close {f.close:,.0f} > {self.p.breakout_bars}d high {f.donchian_high:,.0f}, "
                  f"above EMA{self.p.trend_ema} {f.ema_trend:,.0f}; ATR {f.atr:,.0f}; "
                  f"regime {f.regime.value}")
        return TradeProposal(
            pair=pair, side="buy", order_type="limit", price=entry, qty=qty, intent="entry",
            reason=reason, confidence=conf, stop_loss=sl, target=target, setup="trend_breakout",
        ), "proposed"

    # --------------------------------------------------------------- exits

    def trail_stop(self, position: Position, features: dict[str, TimeframeFeatures],
                   info: PairInfo) -> Decimal | None:
        """New (higher) stop level from closed candles, or None if unchanged."""
        f = features.get(self.tf)
        if f is None or not _finite(f.chandelier_stop):
            return None
        new = info.round_price(_d(f.chandelier_stop), "buy")
        if position.stop_loss is None or new > position.stop_loss:
            return new
        return None

    def propose_exit(
        self,
        position: Position,
        info: PairInfo,
        book: OrderBook,
        features: dict[str, TimeframeFeatures],
    ) -> TradeProposal | None:
        bid = book.best_bid
        if bid is None or position.qty <= 0:
            return None
        qty = info.round_qty(position.qty)
        if qty <= 0:
            return None
        if position.stop_loss is not None and bid <= position.stop_loss:
            return TradeProposal(
                pair=position.pair, side="sell", order_type="market", price=bid, qty=qty, intent="exit",
                reason=f"Stop hit: bid {bid} <= stop {position.stop_loss}",
                confidence=1.0, is_emergency_exit=True, setup="stop_loss",
            )
        if position.take_profit is not None and bid >= position.take_profit:
            return TradeProposal(
                pair=position.pair, side="sell", order_type="limit", price=info.round_price(bid, "sell"),
                qty=qty, intent="exit", reason=f"Take-profit reached: bid {bid} >= TP {position.take_profit}",
                confidence=1.0, setup="take_profit",
            )
        return None
