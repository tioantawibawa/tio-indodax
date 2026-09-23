import pytest

from agent.exchange.pairs import base_asset, quote_asset, to_pair_id, to_symbol, to_ticker_id


@pytest.mark.parametrize("raw", ["btc_idr", "btcidr", "BTCIDR", "BTC/IDR", "btc-idr", " Btc_Idr "])
def test_all_spellings_normalise(raw):
    assert to_ticker_id(raw) == "btc_idr"
    assert to_pair_id(raw) == "btcidr"
    assert to_symbol(raw) == "BTCIDR"


def test_usdt_and_btc_quotes():
    assert to_ticker_id("ethusdt") == "eth_usdt"
    assert to_ticker_id("dogebtc") == "doge_btc"
    assert base_asset("sol_idr") == "sol" and quote_asset("sol_idr") == "idr"


def test_usdt_idr_pair():
    assert to_ticker_id("usdtidr") == "usdt_idr"


@pytest.mark.parametrize("bad", ["", "idr", "btc_xyz", "foo"])
def test_invalid_pairs_raise(bad):
    with pytest.raises(ValueError):
        to_ticker_id(bad)
