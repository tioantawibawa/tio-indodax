"""Typed views of Indodax public API responses.

All prices/quantities are ``Decimal`` (Indodax often sends numbers as strings;
floats would introduce rounding errors in order sizes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Literal

from .errors import IndodaxResponseFormatError
from .pairs import to_ticker_id

Side = Literal["buy", "sell"]


def D(value: Any) -> Decimal:
    """Parse an Indodax number (str/int/float) into Decimal."""
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        raise IndodaxResponseFormatError(f"expected number, got {value!r}")
    try:
        # str() first so floats like 0.1 do not carry binary noise
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise IndodaxResponseFormatError(f"expected number, got {value!r}") from e


def D_opt(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return D(value)


def to_epoch_seconds(value: Any) -> int:
    """Indodax mixes seconds and milliseconds; normalise to seconds."""
    v = int(D(value))
    return v // 1000 if v > 10**12 else v


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class PairInfo:
    ticker_id: str            # btc_idr
    pair_id: str              # btcidr
    symbol: str               # BTCIDR
    base: str                 # traded asset, e.g. btc   (Indodax: traded_currency)
    quote: str                # e.g. idr                 (Indodax: base_currency)
    price_tick: Decimal | None
    qty_step: Decimal | None
    min_quote: Decimal | None  # minimum order value in quote (IDR)
    min_base: Decimal | None   # minimum order size in base asset
    maker_fee_pct: Decimal | None
    taker_fee_pct: Decimal | None
    is_maintenance: bool = False
    is_suspended: bool = False

    @property
    def tradable(self) -> bool:
        return not (self.is_maintenance or self.is_suspended)

    @classmethod
    def from_api(cls, d: dict[str, Any], price_tick: Decimal | None = None) -> "PairInfo":
        try:
            ticker_id = d["ticker_id"]
            pair_id = d["id"]
        except KeyError as e:
            raise IndodaxResponseFormatError(f"pair entry missing {e}") from e
        taker = D_opt(d.get("trade_fee_percent_taker", d.get("trade_fee_percent")))
        return cls(
            ticker_id=ticker_id,
            pair_id=pair_id,
            symbol=d.get("symbol", pair_id.upper()),
            base=d.get("traded_currency", ticker_id.split("_")[0]),
            quote=d.get("base_currency", ticker_id.split("_")[-1]),
            price_tick=price_tick,
            qty_step=D_opt(d.get("quantity_increment")),
            min_quote=D_opt(d.get("trade_min_base_currency")),
            min_base=D_opt(d.get("trade_min_traded_currency")),
            maker_fee_pct=D_opt(d.get("trade_fee_percent_maker")),
            taker_fee_pct=taker,
            is_maintenance=bool(int(d.get("is_maintenance", 0) or 0)),
            is_suspended=bool(int(d.get("is_market_suspended", 0) or 0)),
        )

    def round_price(self, price: Decimal, side: Side) -> Decimal:
        """Snap to tick, always in the conservative direction: buys round down, sells up."""
        if not self.price_tick:
            return price
        if side == "buy":
            return _floor_to_step(price, self.price_tick)
        return _ceil_to_step(price, self.price_tick)

    def round_qty(self, qty: Decimal) -> Decimal:
        """Quantity is always rounded down (never exceed the approved size)."""
        if not self.qty_step:
            return qty
        return _floor_to_step(qty, self.qty_step)

    def min_order_violation(self, price: Decimal, qty: Decimal) -> str | None:
        """Return a reason string if the order is below the exchange minimum."""
        if qty <= 0 or price <= 0:
            return "price and qty must be positive"
        if self.min_base is not None and qty < self.min_base:
            return f"qty {qty} < minimum {self.min_base} {self.base}"
        if self.min_quote is not None and price * qty < self.min_quote:
            return f"order value {price * qty} < minimum {self.min_quote} {self.quote}"
        return None


@dataclass(frozen=True)
class Ticker:
    pair: str
    last: Decimal
    bid: Decimal     # Indodax "buy"  = best bid
    ask: Decimal     # Indodax "sell" = best ask
    high: Decimal
    low: Decimal
    vol_base: Decimal | None
    vol_quote: Decimal | None
    server_time: int  # epoch seconds

    @property
    def spread_pct(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100 if mid > 0 else Decimal("Infinity")

    @classmethod
    def from_api(cls, pair: str, d: dict[str, Any]) -> "Ticker":
        ticker_id = to_ticker_id(pair)
        base, quote = ticker_id.split("_")
        try:
            return cls(
                pair=ticker_id,
                last=D(d["last"]),
                bid=D(d["buy"]),
                ask=D(d["sell"]),
                high=D(d["high"]),
                low=D(d["low"]),
                vol_base=D_opt(d.get(f"vol_{base}")),
                vol_quote=D_opt(d.get(f"vol_{quote}")),
                server_time=to_epoch_seconds(d["server_time"]),
            )
        except KeyError as e:
            raise IndodaxResponseFormatError(f"ticker {pair} missing {e}") from e


@dataclass(frozen=True)
class PublicTrade:
    tid: str
    ts: int  # epoch seconds
    price: Decimal
    amount: Decimal  # base asset
    side: Side

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "PublicTrade":
        try:
            side = d["type"]
            if side not in ("buy", "sell"):
                raise IndodaxResponseFormatError(f"unknown trade type {side!r}")
            return cls(
                tid=str(d["tid"]),
                ts=to_epoch_seconds(d["date"]),
                price=D(d["price"]),
                amount=D(d["amount"]),
                side=side,
            )
        except KeyError as e:
            raise IndodaxResponseFormatError(f"trade missing {e}") from e


@dataclass(frozen=True)
class OrderBook:
    pair: str
    bids: tuple[tuple[Decimal, Decimal], ...]  # (price, base qty), best first (desc)
    asks: tuple[tuple[Decimal, Decimal], ...]  # best first (asc)

    @classmethod
    def from_api(cls, pair: str, d: dict[str, Any]) -> "OrderBook":
        try:
            bids = tuple(sorted(((D(p), D(q)) for p, q in d["buy"]), key=lambda x: -x[0]))
            asks = tuple(sorted(((D(p), D(q)) for p, q in d["sell"]), key=lambda x: x[0]))
        except (KeyError, TypeError, ValueError) as e:
            raise IndodaxResponseFormatError(f"depth {pair} malformed: {e}") from e
        return cls(pair=to_ticker_id(pair), bids=bids, asks=asks)

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> Decimal | None:
        mid = self.mid
        if not mid:
            return None
        return (self.best_ask - self.best_bid) / mid * 100  # type: ignore[operator]

    def estimate_fill(self, side: Side, quote_amount: Decimal) -> "FillEstimate":
        """Walk the book to fill ``quote_amount`` (IDR) immediately.

        ``side='buy'`` consumes asks, ``'sell'`` consumes bids. Slippage is the
        VWAP distance from the best price, in percent.
        """
        levels = self.asks if side == "buy" else self.bids
        if not levels or quote_amount <= 0:
            return FillEstimate(filled_quote=Decimal(0), filled_base=Decimal(0),
                                vwap=None, slippage_pct=None, fully_filled=False)
        remaining = quote_amount
        base = Decimal(0)
        for price, qty in levels:
            level_quote = price * qty
            take = min(level_quote, remaining)
            base += take / price
            remaining -= take
            if remaining <= 0:
                break
        filled_quote = quote_amount - remaining
        vwap = filled_quote / base if base > 0 else None
        best = levels[0][0]
        slip = None
        if vwap is not None:
            slip = (vwap - best) / best * 100 if side == "buy" else (best - vwap) / best * 100
        return FillEstimate(filled_quote=filled_quote, filled_base=base, vwap=vwap,
                            slippage_pct=slip, fully_filled=remaining <= 0)


@dataclass(frozen=True)
class FillEstimate:
    filled_quote: Decimal
    filled_base: Decimal
    vwap: Decimal | None
    slippage_pct: Decimal | None
    fully_filled: bool


@dataclass(frozen=True)
class Candle:
    ts: int  # candle open time, epoch seconds
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Candle":
        try:
            return cls(
                ts=to_epoch_seconds(d["Time"]),
                open=D(d["Open"]),
                high=D(d["High"]),
                low=D(d["Low"]),
                close=D(d["Close"]),
                volume=D(d["Volume"]),
            )
        except KeyError as e:
            raise IndodaxResponseFormatError(f"candle missing {e}") from e


# Timeframe code -> seconds, per /tradingview/history_v2 docs. There is no 5m.
TIMEFRAMES: dict[str, int] = {
    "1": 60,
    "15": 15 * 60,
    "30": 30 * 60,
    "60": 60 * 60,
    "240": 4 * 60 * 60,
    "1D": 24 * 60 * 60,
    "3D": 3 * 24 * 60 * 60,
    "1W": 7 * 24 * 60 * 60,
}


@dataclass(frozen=True)
class Summaries:
    tickers: dict[str, Ticker] = field(default_factory=dict)
    prices_24h: dict[str, Decimal] = field(default_factory=dict)  # keyed by ticker_id
    prices_7d: dict[str, Decimal] = field(default_factory=dict)
