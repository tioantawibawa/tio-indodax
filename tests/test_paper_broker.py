from datetime import timedelta
from decimal import Decimal as D

import pandas as pd
import pytest

from agent.analysis.costs import CostModel
from agent.backtest.paper_broker import PaperBroker
from agent.data.market_data import PairMarket
from agent.portfolio.persistence import rebuild_portfolio, save_stops
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import RiskDecision, Verdict
from agent.storage.db import Database
from tests.helpers import NOW, book, buy, pair_info, sell, settings, ticker


def market(b=None):
    return PairMarket("btc_idr", pair_info(), ticker(), b or book(depth_qty="0.00006"), {"1D": pd.DataFrame()},
                      NOW.timestamp())


def approve(p, qty=None, price=None):
    return RiskDecision(Verdict.APPROVE, p, D(qty or p.qty), D(price or p.price), ("ok",))


def ex(br, dec, m, now, decision_id=None):
    """Journal the decision (as the engine does) and execute it."""
    did = decision_id if decision_id is not None else br.db.record_decision(dec, "paper", ts=now)
    return br.execute(did, dec, m, now)


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "p.db")
    pf = Portfolio(D(1_000_000))
    yield db, pf, PaperBroker(db, pf, CostModel(settings().fees))
    db.close()


def test_marketable_buy_walks_book_with_taker_fee(env):
    db, pf, br = env
    # asks: 1e9 x 0.00006, 1e9+1000 x 0.00006 ...; buy 0.0001 at limit 1e9+1000
    p = buy(price="1000001000", qty="0.0001", sl="930000000")
    [ev] = ex(br, approve(p), market(), NOW)
    assert ev.qty == D("0.0001")
    assert ev.price == (D("0.00006") * D(1_000_000_000) + D("0.00004") * D(1_000_001_000)) / D("0.0001")
    assert ev.fee_idr == pytest.approx(ev.qty * ev.price * D("0.003422"))       # taker + tax + clearing
    assert pf.positions["btc_idr"].stop_loss == D("930000000")
    assert db.get_order(ev.client_order_id)["status"] == "FILLED"


def test_ioc_remainder_cancelled_when_book_thin_within_limit(env):
    db, pf, br = env
    p = buy(price="1000000000", qty="0.0001")   # only first ask level (0.00006) within limit
    [ev] = ex(br, approve(p), market(), NOW)
    assert ev.qty == D("0.00006")
    assert db.get_order(ev.client_order_id)["status"] == "CANCELLED"


def test_same_decision_never_executes_twice(env):
    db, pf, br = env
    p = buy(price="1000000000", qty="0.00005")
    did = db.record_decision(approve(p), "paper", ts=NOW)
    assert ex(br, approve(p), market(), NOW, did)
    assert ex(br, approve(p), market(), NOW, did) == []   # e.g. retry after crash
    assert pf.positions["btc_idr"].qty == D("0.00005")


def test_passive_buy_rests_then_fills_as_maker_or_expires(env):
    db, pf, br = env
    p = buy(price="990000000", qty="0.00005", sl="950000000")
    assert ex(br, approve(p), market(), NOW) == []
    assert br.pending_buy_idr() == {"btc_idr": D("990000000") * D("0.00005")}
    assert br.check_resting({"btc_idr": market()}, NOW + timedelta(minutes=5)) == []   # ask still above
    low = market(book(bid="989000000", ask="990000000", depth_qty="1"))
    [ev] = br.check_resting({"btc_idr": low}, NOW + timedelta(minutes=10))
    assert ev.price == D("990000000") and ev.fee_idr == pytest.approx(D("49500") * D("0.002422"))
    p2 = buy(price="980000000", qty="0.00005")
    ex(br, approve(p2), market(), NOW)
    [o] = br.resting_orders()
    br.check_resting({"btc_idr": market()}, NOW + timedelta(minutes=31))
    assert db.get_order(o["client_order_id"])["status"] == "CANCELLED"


def test_emergency_sell_walks_bids_and_records_stoploss(env):
    db, pf, br = env
    ex(br, approve(buy(price="1000000000", qty="0.00005")), market(), NOW)
    s = sell(price="999000000", qty="0.00005", emergency=True)
    s = s.__class__(**{**s.__dict__, "setup": "stop_loss"})
    [ev] = ex(br, approve(s), market(), NOW + timedelta(minutes=5))
    assert ev.is_stop_loss and ev.price == D("999000000") and "btc_idr" not in pf.positions
    assert "btc_idr" in db.last_stoploss_times("paper")
    assert ev.realized_pnl < 0


def test_sell_never_exceeds_holding(env):
    db, pf, br = env
    ex(br, approve(buy(price="1000000000", qty="0.00003")), market(), NOW)
    [ev] = ex(br, approve(sell(price="999000000", qty="0.0001")), market(), NOW)
    assert ev.qty == D("0.00003") and not pf.positions


def test_cancel_all(env):
    db, pf, br = env
    ex(br, approve(buy(price="990000000", qty="0.00005")), market(), NOW)
    assert br.cancel_all(NOW) == 1 and br.resting_orders() == []


def test_restart_rebuilds_identical_ledger(env):
    db, pf, br = env
    ex(br, approve(buy(price="1000000000", qty="0.00005", sl="930000000")), market(), NOW)
    pf.raise_stop("btc_idr", D("950000000"))
    save_stops(db, "paper", pf)
    ex(br, approve(buy(price="1000000000", qty="0.00001", sl="900000000")), market(), NOW)  # lower stop: ignored
    again = rebuild_portfolio(db, "paper", D(1_000_000))
    assert again.cash_idr == pf.cash_idr and again.fees_paid == pf.fees_paid
    assert again.positions["btc_idr"].qty == pf.positions["btc_idr"].qty
    assert again.positions["btc_idr"].avg_cost == pf.positions["btc_idr"].avg_cost
    assert again.positions["btc_idr"].stop_loss == D("950000000")
