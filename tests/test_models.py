from decimal import Decimal

import pytest

from agent.exchange.errors import IndodaxResponseFormatError
from agent.exchange.models import Candle, OrderBook, PairInfo, PublicTrade, Ticker, to_epoch_seconds
from tests.fixtures import public as fx


def btc_pair() -> PairInfo:
    return PairInfo.from_api(fx.PAIRS[0], price_tick=Decimal("1000"))


def test_pairinfo_parsing():
    p = btc_pair()
    assert (p.ticker_id, p.pair_id, p.symbol, p.base, p.quote) == ("btc_idr", "btcidr", "BTCIDR", "btc", "idr")
    assert p.qty_step == Decimal("0.00000001")
    assert p.min_quote == Decimal("50000") and p.min_base == Decimal("0.0001")
    assert p.tradable
    cat = PairInfo.from_api(fx.PAIRS[1])
    assert cat.maker_fee_pct == Decimal("0.1") and cat.taker_fee_pct == Decimal("0.2")
    assert cat.qty_step == Decimal("1")


def test_price_rounding_is_conservative():
    p = btc_pair()
    assert p.round_price(Decimal("117136999"), "buy") == Decimal("117136000")
    assert p.round_price(Decimal("117136001"), "sell") == Decimal("117137000")
    assert p.round_price(Decimal("117136000"), "sell") == Decimal("117136000")


def test_qty_rounding_never_rounds_up():
    p = btc_pair()
    assert p.round_qty(Decimal("0.123456789")) == Decimal("0.12345678")
    cat = PairInfo.from_api(fx.PAIRS[1])
    assert cat.round_qty(Decimal("295788.99")) == Decimal("295788")


def test_min_order_checks():
    p = btc_pair()
    assert p.min_order_violation(Decimal("100000000"), Decimal("0.00005")) is not None   # below min coin
    assert p.min_order_violation(Decimal("100000000"), Decimal("0.0004")) is not None    # Rp 40k < 50k
    assert p.min_order_violation(Decimal("100000000"), Decimal("0.001")) is None         # Rp 100k
    assert p.min_order_violation(Decimal("0"), Decimal("1")) is not None


def test_suspended_pair_not_tradable():
    d = dict(fx.PAIRS[1], is_market_suspended=1)
    assert not PairInfo.from_api(d).tradable


def test_ticker_parsing_and_spread():
    t = Ticker.from_api("tenidr", fx.TICKER_TEN["ticker"])
    assert t.pair == "ten_idr"
    assert t.bid == Decimal("511") and t.ask == Decimal("512")
    assert t.vol_base == Decimal("153588.49847928") and t.vol_quote == Decimal("78884203")
    assert t.spread_pct == pytest.approx(Decimal(1) / Decimal("511.5") * 100)


def test_ticker_missing_field_raises():
    bad = dict(fx.TICKER_TEN["ticker"]); del bad["last"]
    with pytest.raises(IndodaxResponseFormatError):
        Ticker.from_api("ten_idr", bad)


def test_epoch_seconds_normalisation():
    assert to_epoch_seconds(1571205969552) == 1571205969
    assert to_epoch_seconds("1571207255") == 1571207255


def test_public_trade_parsing():
    t = PublicTrade.from_api(fx.TRADES[0])
    assert t.side == "sell" and t.price == Decimal("511") and t.ts == 1571207255
    with pytest.raises(IndodaxResponseFormatError):
        PublicTrade.from_api(dict(fx.TRADES[0], type="weird"))


def test_orderbook_sorted_and_metrics():
    ob = OrderBook.from_api("tenidr", {"buy": list(reversed(fx.DEPTH["buy"])), "sell": fx.DEPTH["sell"]})
    assert ob.best_bid == Decimal(511) and ob.best_ask == Decimal(512)
    assert ob.mid == Decimal("511.5")
    assert ob.spread_pct == pytest.approx(Decimal(1) / Decimal("511.5") * 100)


def test_orderbook_fill_estimate_single_level_has_zero_slippage():
    ob = OrderBook.from_api("tenidr", fx.DEPTH)
    est = ob.estimate_fill("buy", Decimal(512 * 100))
    assert est.fully_filled and est.slippage_pct == 0
    assert est.filled_base == Decimal(100)


def test_orderbook_fill_estimate_walks_levels():
    ob = OrderBook.from_api("x_idr", {"buy": [[100, "1"], [90, "1"]], "sell": [[100, "1"], [110, "1"]]})
    est = ob.estimate_fill("buy", Decimal(210))           # 1 @100 + 1 @110
    assert est.fully_filled and est.filled_base == Decimal(2)
    assert est.vwap == Decimal(105) and est.slippage_pct == Decimal(5)
    est = ob.estimate_fill("sell", Decimal(190))          # 1 @100 + 1 @90
    assert est.vwap == Decimal(95) and est.slippage_pct == Decimal(5)
    est = ob.estimate_fill("buy", Decimal(10_000))        # book too thin
    assert not est.fully_filled and est.filled_quote == Decimal(210)


def test_orderbook_empty_side():
    ob = OrderBook.from_api("x_idr", {"buy": [], "sell": []})
    assert ob.mid is None and ob.spread_pct is None
    assert not ob.estimate_fill("buy", Decimal(1)).fully_filled


def test_candle_parsing_uses_decimal_from_str():
    c = Candle.from_api(fx.OHLC[0])
    assert c.open == Decimal("0.9999") and c.volume == Decimal("14814.00000000")
