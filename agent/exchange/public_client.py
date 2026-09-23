"""Async client for the Indodax public REST API.

Docs: Public-RestAPI.md in btcid/indodax-official-api-docs
(summarised in docs/indodax_api_notes.md §3).

Features: client-side rate limiting (official limit 180 req/min per IP),
retries with exponential backoff + jitter on network errors / 429 / 5xx,
strict response parsing into Decimal-based models.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import structlog

from .errors import (
    IndodaxAPIError,
    IndodaxNetworkError,
    IndodaxRateLimitError,
    IndodaxResponseFormatError,
)
from .models import (
    TIMEFRAMES,
    Candle,
    OrderBook,
    PairInfo,
    PublicTrade,
    Summaries,
    Ticker,
    D,
)
from .pairs import to_pair_id, to_symbol, to_ticker_id
from .rate_limiter import AsyncRateLimiter

log = structlog.get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
OHLC_MAX_CANDLES_PER_REQUEST = 1000
# Observed (undocumented): the server returns at most the 7 days ending at
# `to` for intraday timeframes, and at most ~730 candles for daily and above,
# whatever `from` is. Page in windows safely inside those caps.
OHLC_INTRADAY_MAX_SPAN_S = 6 * 86400
OHLC_DAILY_MAX_CANDLES = 700


class IndodaxPublicClient:
    def __init__(
        self,
        base_url: str = "https://indodax.com",
        *,
        timeout_s: float = 10.0,
        max_retries: int = 4,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 8.0,
        rate_limit_per_min: int = 150,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._limiter = AsyncRateLimiter(rate_limit_per_min, 60.0, sleep=sleep)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=timeout_s,
            headers={"Accept": "application/json", "User-Agent": "indodax-agent/0.1"},
        )

    @classmethod
    def from_settings(cls, ex, **kw) -> "IndodaxPublicClient":
        return cls(
            ex.public_base_url,
            timeout_s=ex.request_timeout_s,
            max_retries=ex.max_retries,
            backoff_base_s=ex.backoff_base_s,
            backoff_max_s=ex.backoff_max_s,
            rate_limit_per_min=ex.public_rate_limit_per_min,
            **kw,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "IndodaxPublicClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.backoff_max_s)
            except ValueError:
                pass
        delay = min(self.backoff_base_s * (2**attempt), self.backoff_max_s)
        return delay * (0.5 + random.random() / 2)  # jitter in [50%, 100%]

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._limiter.acquire()
            try:
                resp = await self._http.get(url, params=params)
            except httpx.TransportError as e:  # timeouts, connection errors
                last_exc = e
                log.warning("public_api_network_error", path=path, attempt=attempt, error=type(e).__name__)
                if attempt < self.max_retries:
                    await self._sleep(self._backoff(attempt))
                    continue
                raise IndodaxNetworkError(f"GET {path} failed: {type(e).__name__}") from e

            if resp.status_code in RETRYABLE_STATUS:
                log.warning("public_api_retryable_status", path=path, status=resp.status_code, attempt=attempt)
                if attempt < self.max_retries:
                    await self._sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                    continue
                cls = IndodaxRateLimitError if resp.status_code == 429 else IndodaxAPIError
                raise cls(f"GET {path} failed after retries", status=resp.status_code)

            if resp.status_code >= 400:
                raise IndodaxAPIError(
                    f"GET {path}: {_error_text(resp)}", status=resp.status_code
                )
            try:
                data = resp.json()
            except ValueError as e:
                raise IndodaxResponseFormatError(f"GET {path}: response is not JSON") from e
            if isinstance(data, dict) and "error" in data and data.get("success") in (None, 0, "0"):
                raise IndodaxAPIError(
                    f"GET {path}: {data.get('error_description') or data['error']}",
                    status=resp.status_code,
                    code=data.get("error_code") or data.get("error"),
                )
            return data
        raise IndodaxNetworkError(f"GET {path} failed") from last_exc  # pragma: no cover

    # ------------------------------------------------------------- endpoints

    async def server_time_ms(self) -> int:
        data = await self._get("/api/server_time")
        try:
            return int(data["server_time"])
        except (KeyError, TypeError, ValueError) as e:
            raise IndodaxResponseFormatError("server_time malformed") from e

    async def clock_offset_ms(self, now: Callable[[], float] = time.time, samples: int = 5) -> int:
        """Estimate ``server_time - local_time`` (ms).

        Takes several samples and keeps the one with the smallest round trip
        (as NTP does): a single slow request can skew a midpoint estimate by
        hundreds of milliseconds.
        """
        best: tuple[float, int] | None = None
        for _ in range(max(1, samples)):
            t0 = now()
            server = await self.server_time_ms()
            t1 = now()
            rtt = t1 - t0
            offset = server - int((t0 + t1) / 2 * 1000)
            if best is None or rtt < best[0]:
                best = (rtt, offset)
        return best[1]  # type: ignore[index]

    async def price_increments(self) -> dict[str, Decimal]:
        data = await self._get("/api/price_increments")
        try:
            return {k: D(v) for k, v in data["increments"].items()}
        except (KeyError, AttributeError) as e:
            raise IndodaxResponseFormatError("price_increments malformed") from e

    async def pairs(self, with_price_increments: bool = True) -> dict[str, PairInfo]:
        """All pairs keyed by ticker_id. Tick size comes from /api/price_increments."""
        data = await self._get("/api/pairs")
        if not isinstance(data, list):
            raise IndodaxResponseFormatError("/api/pairs did not return a list")
        ticks = await self.price_increments() if with_price_increments else {}
        out: dict[str, PairInfo] = {}
        for entry in data:
            tid = entry.get("ticker_id")
            if not tid:
                continue
            out[tid] = PairInfo.from_api(entry, price_tick=ticks.get(tid))
        return out

    async def ticker(self, pair: str) -> Ticker:
        data = await self._get(f"/api/ticker/{to_pair_id(pair)}")
        try:
            return Ticker.from_api(pair, data["ticker"])
        except (KeyError, TypeError) as e:
            raise IndodaxResponseFormatError(f"ticker {pair} malformed") from e

    async def ticker_all(self) -> dict[str, Ticker]:
        data = await self._get("/api/ticker_all")
        try:
            return {k: Ticker.from_api(k, v) for k, v in data["tickers"].items()}
        except (KeyError, AttributeError) as e:
            raise IndodaxResponseFormatError("ticker_all malformed") from e

    async def summaries(self) -> Summaries:
        data = await self._get("/api/summaries")
        try:
            tickers = {k: Ticker.from_api(k, v) for k, v in data["tickers"].items()}
        except (KeyError, AttributeError) as e:
            raise IndodaxResponseFormatError("summaries malformed") from e
        return Summaries(
            tickers=tickers,
            prices_24h=_by_ticker_id(data.get("prices_24h", {})),
            prices_7d=_by_ticker_id(data.get("prices_7d", {})),
        )

    async def trades(self, pair: str) -> list[PublicTrade]:
        data = await self._get(f"/api/trades/{to_pair_id(pair)}")
        if not isinstance(data, list):
            raise IndodaxResponseFormatError(f"trades {pair} did not return a list")
        return [PublicTrade.from_api(t) for t in data]

    async def depth(self, pair: str) -> OrderBook:
        data = await self._get(f"/api/depth/{to_pair_id(pair)}")
        return OrderBook.from_api(pair, data)

    async def ohlc(self, pair: str, timeframe: str, start: int, end: int) -> list[Candle]:
        """Candles with open time in [start, end] (epoch seconds), ascending, deduplicated.

        The range is split into chunks: at most 6 days per request for
        intraday timeframes and at most 700 candles for daily+ (the server
        silently returns only the last 7 days / ~730 candles of a longer window).
        """
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {timeframe!r}; valid: {list(TIMEFRAMES)}")
        if end < start:
            raise ValueError("end must be >= start")
        step = TIMEFRAMES[timeframe]
        chunk = step * OHLC_MAX_CANDLES_PER_REQUEST
        if step < 86400:
            chunk = min(chunk, OHLC_INTRADAY_MAX_SPAN_S)
        else:
            chunk = min(chunk, step * OHLC_DAILY_MAX_CANDLES)
        symbol = to_symbol(pair)
        candles: dict[int, Candle] = {}
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + chunk - 1, end)
            data = await self._get(
                "/tradingview/history_v2",
                params={"from": cursor, "symbol": symbol, "tf": timeframe, "to": chunk_end},
            )
            if not isinstance(data, list):
                raise IndodaxResponseFormatError(f"ohlc {pair} did not return a list")
            for row in data:
                c = Candle.from_api(row)
                if start <= c.ts <= end:
                    candles[c.ts] = c
            cursor = chunk_end + 1
        return [candles[k] for k in sorted(candles)]


def _by_ticker_id(d: dict[str, Any]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for k, v in d.items():
        try:
            out[to_ticker_id(k)] = D(v)
        except (ValueError, IndodaxResponseFormatError):
            continue  # unknown quote currency or bad value: skip, don't fail the whole call
    return out


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("error_description") or data.get("error") or data.get("msg") or data)
    except ValueError:
        pass
    return resp.text[:200]
