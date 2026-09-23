import httpx
import pytest
import respx
from decimal import Decimal

from agent.exchange.errors import (
    IndodaxAPIError,
    IndodaxNetworkError,
    IndodaxRateLimitError,
    IndodaxResponseFormatError,
)
from agent.exchange.public_client import IndodaxPublicClient
from tests.fixtures import public as fx

BASE = "https://indodax.com"


async def _nosleep(_s: float) -> None:
    return None


@pytest.fixture
async def client():
    c = IndodaxPublicClient(BASE, max_retries=3, sleep=_nosleep)
    yield c
    await c.aclose()


@respx.mock
async def test_server_time_and_offset(client):
    respx.get(f"{BASE}/api/server_time").mock(return_value=httpx.Response(200, json=fx.SERVER_TIME))
    assert await client.server_time_ms() == 1571205969552
    ticks = iter([1571205969.0, 1571205969.2])  # local midpoint = ...969100 ms
    assert await client.clock_offset_ms(now=lambda: next(ticks)) == 452


@respx.mock
async def test_pairs_merges_price_increments(client):
    respx.get(f"{BASE}/api/pairs").mock(return_value=httpx.Response(200, json=fx.PAIRS))
    respx.get(f"{BASE}/api/price_increments").mock(return_value=httpx.Response(200, json=fx.PRICE_INCREMENTS))
    pairs = await client.pairs()
    assert set(pairs) == {"btc_idr", "cat_idr"}
    assert pairs["btc_idr"].price_tick == Decimal("1000")
    assert pairs["cat_idr"].price_tick == Decimal("0.000001")


@respx.mock
async def test_ticker_uses_pair_id_in_path(client):
    route = respx.get(f"{BASE}/api/ticker/tenidr").mock(return_value=httpx.Response(200, json=fx.TICKER_TEN))
    t = await client.ticker("ten_idr")
    assert route.called and t.pair == "ten_idr" and t.last == Decimal("511")


@respx.mock
async def test_ticker_all_and_summaries(client):
    respx.get(f"{BASE}/api/ticker_all").mock(return_value=httpx.Response(200, json=fx.TICKER_ALL))
    respx.get(f"{BASE}/api/summaries").mock(return_value=httpx.Response(200, json=fx.SUMMARIES))
    ta = await client.ticker_all()
    assert ta["btc_idr"].vol_quote == Decimal("25800033297")
    s = await client.summaries()
    assert s.tickers["btc_idr"].bid == Decimal("116938000")
    assert s.prices_24h["btc_idr"] == Decimal("120002000")  # btcidr -> btc_idr
    assert s.prices_7d["ten_idr"] == Decimal("517")


@respx.mock
async def test_trades_and_depth(client):
    respx.get(f"{BASE}/api/trades/btcidr").mock(return_value=httpx.Response(200, json=fx.TRADES))
    respx.get(f"{BASE}/api/depth/btcidr").mock(return_value=httpx.Response(200, json=fx.DEPTH))
    trades = await client.trades("btc_idr")
    assert [t.tid for t in trades] == ["1623490", "1623489"]
    ob = await client.depth("BTCIDR")
    assert ob.pair == "btc_idr" and ob.best_ask == Decimal(512)


@respx.mock
async def test_ohlc_params_and_chunking(client):
    route = respx.get(f"{BASE}/tradingview/history_v2").mock(return_value=httpx.Response(200, json=fx.OHLC))
    start, end = 1699328700, 1699330500
    candles = await client.ohlc("btc_idr", "15", start, end)
    assert route.call_count == 1
    q = route.calls[0].request.url.params
    assert q["symbol"] == "BTCIDR" and q["tf"] == "15" and q["from"] == str(start) and q["to"] == str(end)
    assert [c.ts for c in candles] == [1699328700, 1699329600, 1699330500]

    # 1-minute candles over 2500 minutes -> 3 chunks of <=1000 candles; overlap is deduped
    route.reset()
    candles = await client.ohlc("btc_idr", "1", start, start + 2500 * 60)
    assert route.call_count == 3
    assert len(candles) == 3  # same fixture returned each time -> deduplicated

    # intraday windows are capped at 6 days (server returns only the last 7 days otherwise)
    route.reset()
    await client.ohlc("btc_idr", "240", start, start + 30 * 86400 - 1)  # exactly 30 days
    assert route.call_count == 5
    for call in route.calls:
        q = call.request.url.params
        assert int(q["to"]) - int(q["from"]) < 7 * 86400
    route.reset()
    await client.ohlc("btc_idr", "1D", start, start + 200 * 86400)  # daily: <=700-candle chunks
    assert route.call_count == 1
    route.reset()
    await client.ohlc("btc_idr", "1D", start, start + 1400 * 86400 - 1)
    assert route.call_count == 2


async def test_ohlc_rejects_unsupported_timeframe(client):
    with pytest.raises(ValueError):
        await client.ohlc("btc_idr", "5", 0, 100)  # Indodax has no 5m timeframe
    with pytest.raises(ValueError):
        await client.ohlc("btc_idr", "15", 100, 0)


@respx.mock
async def test_retries_on_5xx_then_succeeds(client):
    route = respx.get(f"{BASE}/api/server_time").mock(side_effect=[
        httpx.Response(502), httpx.Response(503), httpx.Response(200, json=fx.SERVER_TIME),
    ])
    assert await client.server_time_ms() == 1571205969552
    assert route.call_count == 3


@respx.mock
async def test_retries_on_network_error_then_raises(client):
    route = respx.get(f"{BASE}/api/server_time").mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(IndodaxNetworkError):
        await client.server_time_ms()
    assert route.call_count == 4  # 1 + max_retries


@respx.mock
async def test_429_exhausted_raises_rate_limit_error(client):
    respx.get(f"{BASE}/api/server_time").mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(IndodaxRateLimitError):
        await client.server_time_ms()


@respx.mock
async def test_4xx_not_retried(client):
    route = respx.get(f"{BASE}/api/ticker/xxxidr").mock(
        return_value=httpx.Response(404, json={"error": "invalid_pair", "error_description": "Invalid pair"}))
    with pytest.raises(IndodaxAPIError) as ei:
        await client.ticker("xxx_idr")
    assert route.call_count == 1 and ei.value.status == 404


@respx.mock
async def test_error_payload_with_200_is_raised(client):
    respx.get(f"{BASE}/api/ticker/xxxidr").mock(
        return_value=httpx.Response(200, json={"error": "invalid_pair", "error_description": "Invalid pair"}))
    with pytest.raises(IndodaxAPIError, match="Invalid pair"):
        await client.ticker("xxx_idr")


@respx.mock
async def test_non_json_and_wrong_shape(client):
    respx.get(f"{BASE}/api/server_time").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(IndodaxResponseFormatError):
        await client.server_time_ms()
    respx.get(f"{BASE}/api/trades/btcidr").mock(return_value=httpx.Response(200, json={"oops": 1}))
    with pytest.raises(IndodaxResponseFormatError):
        await client.trades("btc_idr")


def test_backoff_bounded(client):
    for attempt in range(10):
        d = client._backoff(attempt)
        assert 0 < d <= client.backoff_max_s
    assert client._backoff(0, retry_after="3") == 3
