from decimal import Decimal as D

from agent.data.market_data import MarketDataService, candles_to_df, closed_only
from agent.exchange.models import Candle
from tests.helpers import book, pair_info, ticker


def c(ts, close=1.0):
    return Candle(ts=ts, open=D(close), high=D(close), low=D(close), close=D(close), volume=D(1))


def test_closed_only_drops_forming_candle():
    cs = [c(0), c(900), c(1800)]
    assert [x.ts for x in closed_only(cs, "15", now_s=2000)] == [0, 900]
    assert [x.ts for x in closed_only(cs, "15", now_s=2700)] == [0, 900, 1800]


def test_candles_to_df():
    df = candles_to_df([c(900, 2.0), c(0, 1.0), c(900, 3.0)])
    assert list(df["close"]) == [1.0, 3.0] and str(df.index.tz) == "UTC"
    assert candles_to_df([]).empty


class FakeClient:
    def __init__(self):
        self.ohlc_calls = 0

    async def pairs(self):
        return {"btc_idr": pair_info()}

    async def ticker(self, pair):
        return ticker()

    async def depth(self, pair):
        return book()

    async def ohlc(self, pair, tf, start, end):
        self.ohlc_calls += 1
        step = {"15": 900, "60": 3600}[tf]
        return [c(t) for t in range(start - start % step, end + 1, step)]


async def test_fetch_and_candle_cache():
    now = [108_000.0]  # exactly on an hour boundary
    fc = FakeClient()
    svc = MarketDataService(fc, ["15", "60"], lookback_bars=100, now=lambda: now[0])
    m = await svc.fetch("btc_idr")
    assert set(m.candles) == {"15", "60"} and fc.ohlc_calls == 2
    assert len(m.candles["15"]) <= 100
    assert m.candles["15"].index[-1].timestamp() + 900 <= now[0]  # no forming candle
    now[0] += 60           # same 15m bucket -> cached
    await svc.fetch("btc_idr")
    assert fc.ohlc_calls == 2
    now[0] += 900          # new 15m candle closed, 1h not yet
    await svc.fetch("btc_idr")
    assert fc.ohlc_calls == 3
