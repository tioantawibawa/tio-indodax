"""Async sliding-window rate limiter."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable


class AsyncRateLimiter:
    """Allow at most ``max_calls`` acquisitions in any ``period_s`` window."""

    def __init__(
        self,
        max_calls: int,
        period_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if max_calls <= 0 or period_s <= 0:
            raise ValueError("max_calls and period_s must be positive")
        self.max_calls = max_calls
        self.period_s = period_s
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.period_s:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                await self._sleep(self.period_s - (now - self._calls[0]))

    async def __aenter__(self) -> "AsyncRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        return None
