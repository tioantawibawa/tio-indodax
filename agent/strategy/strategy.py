"""Strategy: turns analysed market data into TradeProposals.

Long-only spot. Two entry setups, both gated by the higher-timeframe regime:

1. Trend pullback — trend TF (4h) not trending down, signal TF (1h)
   TRENDING_UP; on the entry TF (15m) RSI has cooled off (40–65) and MACD
   histogram is turning up. SL = entry − sl_atr_mult × ATR(1h),
   TP = entry + tp_atr_mult × ATR(1h).
2. Range mean-reversion — signal TF RANGING, trend TF not trending down,
   entry TF closes at/below the lower Bollinger band with RSI < mr_rsi_max.
   SL = entry − mr_sl_atr_mult × ATR(1h), TP = 1h Bollinger mid.

Exits: stop-loss hit (emergency market exit), take-profit hit, or the signal
TF turning TRENDING_DOWN / HIGH_VOLATILITY (limit exit).

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

from agent.analysis.regime import Regime
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
    take_profit: Decimal | None = None
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
        self.entry_tf, self.signal_tf, self.trend_tf = params.timeframes

    # ------------------------------------------------------------- entries

    def propose_entry(
        self,
        pair: str,
        info: PairInfo,
        book: OrderBook,
        features: dict[str, TimeframeFeatures],
    ) -> tuple[TradeProposal | None, str]:
        """Return (proposal, note). ``note`` explains why nothing was proposed."""
        e, s, t = (features.get(tf) for tf in (self.entry_tf, self.signal_tf, self.trend_tf))
        if e is None or s is None or t is None:
            return None, "missing timeframe data"
        if book.best_bid is None or book.best_ask is None:
            return None, "empty orderbook"
        if t.regime in (Regime.TRENDING_DOWN, Regime.HIGH_VOLATILITY, Regime.NO_TRADE):
            return None, f"trend TF regime {t.regime.value}: {t.regime_reason}"
        if not _finite(e.rsi, e.macd_hist, e.macd_hist_prev, e.close, e.bb_lower, s.atr, s.bb_mid, s.adx):
            return None, "indicators not ready"

        entry = info.round_price(book.best_bid, "buy")  # rest on the bid: maker
        atr = _d(s.atr)
        setup = None
        if s.regime == Regime.TRENDING_UP:
            pullback = 40 <= e.rsi <= 65
            turning_up = e.macd_hist > e.macd_hist_prev
            if not (pullback and turning_up):
                return None, f"trend up but no pullback entry (RSI15 {e.rsi:.1f}, MACD hist rising={turning_up})"
            setup = "trend_pullback"
            sl = entry - _d(self.p.sl_atr_mult) * atr
            tp = entry + _d(self.p.tp_atr_mult) * atr
            conf = 0.55 + min((s.adx - self.p.adx_trend) / 100, 0.15)
            if t.regime == Regime.TRENDING_UP:
                conf += 0.1
            reason = (f"Trend pullback: 1h {s.regime_reason}; 4h {t.regime.value}; "
                      f"15m RSI {e.rsi:.1f}, MACD hist {e.macd_hist_prev:.4g}->{e.macd_hist:.4g}")
        elif s.regime == Regime.RANGING:
            if not (e.close <= e.bb_lower * 1.002 and e.rsi < self.p.mr_rsi_max):
                return None, f"ranging but not oversold (RSI15 {e.rsi:.1f}, %B {e.bb_pctb:.2f})"
            setup = "range_reversion"
            sl = entry - _d(self.p.mr_sl_atr_mult) * atr
            tp = _d(s.bb_mid)
            conf = 0.55 + min((self.p.mr_rsi_max - e.rsi) / 100, 0.15)
            reason = (f"Range reversion: 1h {s.regime_reason}; 15m close at lower BB, "
                      f"RSI {e.rsi:.1f}; target 1h BB mid")
        else:
            return None, f"signal TF regime {s.regime.value}: {s.regime_reason}"

        sl = info.round_price(sl, "buy")      # round SL down (further away = conservative)
        tp = info.round_price(tp, "sell")
        if sl <= 0 or sl >= entry:
            return None, "invalid stop-loss level"
        if tp <= entry:
            return None, f"take-profit {tp} not above entry {entry}"
        conf = round(max(0.0, min(conf, 0.95)), 3)
        if conf < self.p.min_confidence:
            return None, f"confidence {conf} < {self.p.min_confidence}"

        # Risk-based sizing: lose ~risk_per_trade_pct of capital if SL hits.
        risk_idr = self.capital_idr * _d(self.p.risk_per_trade_pct) / 100
        qty = info.round_qty(risk_idr / (entry - sl))
        if qty <= 0:
            return None, "position size rounds to zero"
        return TradeProposal(
            pair=pair, side="buy", order_type="limit", price=entry, qty=qty, intent="entry",
            reason=reason, confidence=conf, stop_loss=sl, take_profit=tp, setup=setup,
        ), "proposed"

    # --------------------------------------------------------------- exits

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
        pair = position.pair
        qty = info.round_qty(position.qty)
        if qty <= 0:
            return None
        if position.stop_loss is not None and bid <= position.stop_loss:
            return TradeProposal(
                pair=pair, side="sell", order_type="market", price=bid, qty=qty, intent="exit",
                reason=f"Stop-loss hit: bid {bid} <= SL {position.stop_loss}",
                confidence=1.0, is_emergency_exit=True, setup="stop_loss",
            )
        if position.take_profit is not None and bid >= position.take_profit:
            return TradeProposal(
                pair=pair, side="sell", order_type="limit", price=info.round_price(bid, "sell"),
                qty=qty, intent="exit", reason=f"Take-profit reached: bid {bid} >= TP {position.take_profit}",
                confidence=1.0, setup="take_profit",
            )
        s = features.get(self.signal_tf)
        if s is not None and s.regime in (Regime.TRENDING_DOWN, Regime.HIGH_VOLATILITY):
            return TradeProposal(
                pair=pair, side="sell", order_type="limit", price=info.round_price(bid, "sell"),
                qty=qty, intent="exit", reason=f"Signal TF turned {s.regime.value}: {s.regime_reason}",
                confidence=0.8, setup="regime_exit",
            )
        return None
