import pytest

from agent.exchange.rate_limiter import AsyncRateLimiter


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


async def test_allows_burst_up_to_limit_then_waits():
    ft = FakeTime()
    rl = AsyncRateLimiter(3, 60, clock=ft.clock, sleep=ft.sleep)
    for _ in range(3):
        await rl.acquire()
    assert ft.sleeps == []
    await rl.acquire()
    assert ft.sleeps == [60]


async def test_window_slides():
    ft = FakeTime()
    rl = AsyncRateLimiter(2, 10, clock=ft.clock, sleep=ft.sleep)
    await rl.acquire(); ft.t = 5; await rl.acquire()
    ft.t = 10.5  # first call expired
    await rl.acquire()
    assert ft.sleeps == []
    await rl.acquire()  # must wait until t=15 (second call expires)
    assert ft.t == pytest.approx(15)


def test_invalid_args():
    with pytest.raises(ValueError):
        AsyncRateLimiter(0, 1)
