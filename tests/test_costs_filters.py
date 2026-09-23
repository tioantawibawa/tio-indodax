from decimal import Decimal as D

import pytest

from agent.analysis.costs import CostModel
from agent.analysis.filters import check_market
from tests.helpers import book, pair_info, settings, ticker


def test_round_trip_cost_components():
    s = settings()
    cm = CostModel(s.fees)
    c = cm.round_trip(pair_info(), spread_pct=D("0.1"), slippage_pct=D("0.05"))
    assert c.entry_fee_pct == D("0.1") and c.exit_fee_pct == D("0.2")
    assert c.tax_clearing_pct == D("0.2844")
    assert c.total_pct == D("0.7344")


def test_fallback_fees_when_pair_has_none():
    s = settings()
    info = pair_info()
    from dataclasses import replace
    info = replace(info, maker_fee_pct=None, taker_fee_pct=None)
    cm = CostModel(s.fees)
    assert cm.leg_fee_pct(info, maker=False) == D("0.2") + D("0.12") + D("0.0222")
    assert cm.fee_idr(info, D(100_000), maker=True) == pytest.approx(D("242.2"))


def test_negative_spread_clamped():
    cm = CostModel(settings().fees)
    assert cm.round_trip(None, D("-1"), D("-1")).spread_pct == 0


def test_market_filter_ok_and_failures():
    s = settings()
    ok = check_market("btc_idr", pair_info(), ticker(), book(), D(100_000), "buy", s.market)
    assert ok.ok and ok.reasons == ()
    bad = check_market("doge_idr", pair_info("doge_idr"), ticker(vol_quote="1"),
                       book(bid="900000000"), D(100_000), "buy", s.market)
    assert not bad.ok
    joined = " ".join(bad.reasons)
    assert "whitelist" in joined and "volume" in joined and "spread" in joined


def test_market_filter_thin_book():
    s = settings()
    r = check_market("btc_idr", pair_info(), ticker(), book(depth_qty="0.00001", levels=1),
                     D(100_000), "buy", s.market)
    assert not r.ok and "thin" in r.reasons[0]
