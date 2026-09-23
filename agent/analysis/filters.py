"""Market filter: whitelist + liquidity (volume, spread, orderbook depth)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from agent.config import MarketSettings
from agent.exchange.models import OrderBook, PairInfo, Side, Ticker


@dataclass(frozen=True)
class LiquidityResult:
    ok: bool
    spread_pct: Decimal | None
    slippage_pct: Decimal | None
    volume_24h_idr: Decimal | None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def check_market(
    pair: str,
    info: PairInfo,
    ticker: Ticker,
    book: OrderBook,
    order_quote_idr: Decimal,
    side: Side,
    m: MarketSettings,
) -> LiquidityResult:
    reasons: list[str] = []
    if pair not in m.whitelist:
        reasons.append(f"{pair} not in whitelist")
    if not info.tradable:
        reasons.append(f"{pair} in maintenance/suspended")
    vol = ticker.vol_quote
    if vol is None or vol < Decimal(str(m.min_volume_24h_idr)):
        reasons.append(f"24h volume {vol} < min {m.min_volume_24h_idr:,.0f} IDR")
    spread = book.spread_pct
    if spread is None:
        reasons.append("orderbook empty")
    elif spread > Decimal(str(m.max_spread_pct)):
        reasons.append(f"spread {spread:.3f}% > max {m.max_spread_pct}%")
    est = book.estimate_fill(side, order_quote_idr)
    slip = est.slippage_pct
    if not est.fully_filled:
        reasons.append(f"orderbook too thin to fill {order_quote_idr:,.0f} IDR")
    elif slip is not None and slip > Decimal(str(m.max_slippage_pct)):
        reasons.append(f"estimated slippage {slip:.3f}% > max {m.max_slippage_pct}%")
    return LiquidityResult(ok=not reasons, spread_pct=spread, slippage_pct=slip,
                           volume_24h_idr=vol, reasons=tuple(reasons))
