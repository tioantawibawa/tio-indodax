from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from agent.portfolio.portfolio import Portfolio

T = datetime(2026, 9, 23, tzinfo=timezone.utc)


def test_buy_sets_avg_cost_including_fee():
    p = Portfolio(D(1_000_000))
    p.apply_fill("btc_idr", "buy", D("0.0001"), D("1000000000"), D("100"), T, D("980000000"), D("1040000000"))
    assert p.cash_idr == D(1_000_000) - D(100_000) - D(100)
    pos = p.positions["btc_idr"]
    assert pos.avg_cost == D("1001000000")
    assert pos.stop_loss == D("980000000")


def test_average_up_and_partial_sell_realizes_pnl():
    p = Portfolio(D(1_000_000))
    p.apply_fill("btc_idr", "buy", D("0.0001"), D("1000000000"), D(0), T)
    p.apply_fill("btc_idr", "buy", D("0.0001"), D("1100000000"), D(0), T)
    assert p.positions["btc_idr"].avg_cost == D("1050000000")
    r = p.apply_fill("btc_idr", "sell", D("0.0001"), D("1200000000"), D(50), T)
    assert r.realized_pnl == D(15_000) - D(50)
    assert p.positions["btc_idr"].qty == D("0.0001")
    r = p.apply_fill("btc_idr", "sell", D("0.0001"), D("1000000000"), D(0), T)
    assert r.position is None and "btc_idr" not in p.positions
    assert p.realized_pnl == D(14950) - D(5000)
    assert p.cash_idr == D(1_000_000) + p.realized_pnl
    assert p.fees_paid == D(50)


def test_no_shorting_and_no_overspending():
    p = Portfolio(D(10_000))
    with pytest.raises(ValueError):
        p.apply_fill("btc_idr", "sell", D("0.1"), D(1), D(0), T)
    with pytest.raises(ValueError):
        p.apply_fill("btc_idr", "buy", D("1"), D(20_000), D(0), T)
    with pytest.raises(ValueError):
        p.apply_fill("btc_idr", "buy", D("0"), D(1), D(0), T)


def test_equity_unrealized_exposure():
    p = Portfolio(D(1_000_000))
    p.apply_fill("btc_idr", "buy", D("0.0001"), D("1000000000"), D(0), T)
    p.apply_fill("btc_usdt", "buy", D("0.0001"), D("500000000"), D(0), T)
    prices = {"btc_idr": D("1100000000"), "btc_usdt": D("500000000")}
    assert p.equity(prices) == D(850_000) + D(110_000) + D(50_000)
    assert p.unrealized_pnl(prices) == D(10_000)
    assert p.exposure_by_asset(prices) == {"btc": D(160_000)}
