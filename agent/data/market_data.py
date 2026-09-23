"""Market data collection: ticker, orderbook and multi-timeframe candles.

Candle fetches are cached per (pair, timeframe) and refreshed only when a new
candle has closed, which keeps the public API well under its rate limit
(3 pairs x ~3 requests per 5-minute cycle).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pandas as pd
import structlog

from agent.exchange.models import TIMEFRAMES, Candle, OrderBook, PairInfo, Ticker
from agent.exchange.public_client import IndodaxPublicClient

log = structlog.get_logger(__name__)


def candles_to_df(candles: Sequence[Candle]) -> pd.DataFrame:
    """Decimal candles -> float OHLCV DataFrame indexed by UTC open time."""
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"), dtype=float)
    df = pd.DataFrame(
        {
            "open": [float(c.open) for c in candles],
            "high": [float(c.high) for c in candles],
            "low": [float(c.low) for c in candles],
            "close": [float(c.close) for c in candles],
            "volume": [float(c.volume) for c in candles],
        },
        index=pd.to_datetime([c.ts for c in candles], unit="s", utc=True),
    )
    df.index.name = "ts"
    return df[~df.index.duplicated(keep="last")].sort_index()


def closed_only(candles: Sequence[Candle], timeframe: str, now_s: float) -> list[Candle]:
    """Drop the still-forming candle (open_time + duration > now)."""
    dur = TIMEFRAMES[timeframe]
    return [c for c in candles if c.ts + dur <= now_s]


@dataclass(frozen=True)
class PairMarket:
    """Everything the analysis layer needs for one pair, fetched in one cycle."""

    pair: str
    info: PairInfo
    ticker: Ticker
    orderbook: OrderBook
    candles: dict[str, pd.DataFrame]  # timeframe -> closed candles
    fetched_at: float


class StaleMarketDataError(RuntimeError):
    """Ticker older than allowed: never trade on it."""


class MarketDataService:
    def __init__(
        self,
        client: IndodaxPublicClient,
        timeframes: Sequence[str],
        lookback_bars: int = 200,
        now: Callable[[], float] = time.time,
        max_staleness_s: float = 60.0,
    ):
        self.client = client
        self.max_staleness_s = max_staleness_s
        self.timeframes = tuple(timeframes)
        self.lookback_bars = lookback_bars
        self._now = now
        self._pairs: dict[str, PairInfo] = {}
        self._pairs_fetched_at = 0.0
        self._candle_cache: dict[tuple[str, str], tuple[float, list[Candle]]] = {}

    async def pair_info(self, pair: str, max_age_s: float = 3600) -> PairInfo:
        if not self._pairs or self._now() - self._pairs_fetched_at > max_age_s:
            self._pairs = await self.client.pairs()
            self._pairs_fetched_at = self._now()
        try:
            return self._pairs[pair]
        except KeyError:
            raise KeyError(f"pair {pair} not listed on Indodax") from None

    async def candles(self, pair: str, timeframe: str) -> list[Candle]:
        now = self._now()
        dur = TIMEFRAMES[timeframe]
        key = (pair, timeframe)
        cached = self._candle_cache.get(key)
        # Refetch only if a newer candle should have closed since the last fetch.
        if cached and (now // dur) == (cached[0] // dur):
            return cached[1]
        start = int(now - dur * (self.lookback_bars + 2))
        fetched = await self.client.ohlc(pair, timeframe, start, int(now))
        closed = closed_only(fetched, timeframe, now)[-self.lookback_bars:]
        self._candle_cache[key] = (now, closed)
        return closed

    async def fetch(self, pair: str) -> PairMarket:
        info = await self.pair_info(pair)
        ticker = await self.client.ticker(pair)
        age = self._now() - ticker.server_time
        if age > self.max_staleness_s:
            raise StaleMarketDataError(f"{pair} ticker is {age:.0f}s old (max {self.max_staleness_s}s)")
        book = await self.client.depth(pair)
        frames = {tf: candles_to_df(await self.candles(pair, tf)) for tf in self.timeframes}
        return PairMarket(pair=pair, info=info, ticker=ticker, orderbook=book,
                          candles=frames, fetched_at=self._now())
