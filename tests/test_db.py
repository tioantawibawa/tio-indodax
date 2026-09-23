from datetime import timedelta
from decimal import Decimal as D

import pytest

from agent.risk.risk_manager import RiskManager, Verdict
from agent.storage.db import Database
from tests.helpers import NOW, buy, ctx, market_view, settings


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def test_decisions_including_veto_are_journaled(db):
    s = settings()
    rm = RiskManager(s.risk, s.market, s.fees)
    ok = rm.evaluate(buy(), ctx(), market_view())
    veto = rm.evaluate(buy(sl=None), ctx(), market_view())
    assert veto.verdict == Verdict.VETO
    db.record_decision(ok, "paper", ts=NOW)
    db.record_decision(veto, "paper", {"note": "x"}, ts=NOW)
    rows = db.decisions_between(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert [r["verdict"] for r in rows] == ["APPROVE", "VETO"]
    assert "stop-loss" in rows[1]["reasons_json"]
    assert rows[1]["llm_json"] is not None and D(rows[0]["price"]) == D("999000000")


def test_order_counting_windows_and_emergency_exemption(db):
    for i, (mins, emerg, status) in enumerate([(5, False, "NEW"), (30, False, "FILLED"), (90, False, "CANCELLED"),
                                               (10, True, "FILLED"), (2, False, "REJECTED")]):
        db.record_order(client_order_id=f"c{i}", decision_id=None, mode="paper", pair="btc_idr", side="buy",
                        order_type="limit", price=D(1), qty=D(1), status=status, is_emergency=emerg,
                        ts=NOW - timedelta(minutes=mins))
    assert db.count_orders_since(NOW - timedelta(hours=1), "paper") == 2
    assert db.count_orders_since(NOW - timedelta(hours=2), "paper") == 3
    assert db.count_orders_since(NOW - timedelta(hours=1), "paper", include_emergency=True) == 3
    assert db.count_orders_since(NOW - timedelta(hours=1), "live") == 0


def test_client_order_id_unique(db):
    kw = dict(decision_id=None, mode="paper", pair="btc_idr", side="buy", order_type="limit", price=D(1), qty=D(1))
    db.record_order(client_order_id="dup", **kw)
    with pytest.raises(Exception):
        db.record_order(client_order_id="dup", **kw)
    db.update_order("dup", status="FILLED", exchange_order_id="123", filled_qty=D(1))
    row = db.get_order("dup")
    assert row["status"] == "FILLED" and row["exchange_order_id"] == "123"
    assert db.open_orders("paper") == []


def test_fill_idempotent(db):
    kw = dict(trade_id="t1", client_order_id="c", ts=NOW, mode="paper", pair="btc_idr", side="buy",
              price=D(1), qty=D(1), fee_idr=D(0))
    assert db.record_fill(**kw) is True
    assert db.record_fill(**kw) is False
    assert len(db.fills_between(NOW - timedelta(seconds=1), NOW + timedelta(seconds=1), "paper")) == 1


def test_stoploss_times(db):
    db.record_stoploss("btc_idr", "paper", NOW - timedelta(minutes=40))
    db.record_stoploss("btc_idr", "paper", NOW - timedelta(minutes=5))
    assert db.last_stoploss_times("paper") == {"btc_idr": NOW - timedelta(minutes=5)}


def test_daily_pnl_and_paper_days(db):
    db.upsert_daily_pnl("2026-09-22", "paper", start_equity=D(100))
    assert db.paper_days_recorded() == 0
    db.upsert_daily_pnl("2026-09-22", "paper", start_equity=D(999), end_equity=D(105))
    row = db.get_daily_pnl("2026-09-22", "paper")
    assert row["start_equity"] == "100" and row["end_equity"] == "105"  # start not overwritten
    assert db.paper_days_recorded() == 1


def test_state_and_errors_and_persistence(tmp_path):
    p = tmp_path / "s.db"
    db = Database(p)
    db.set_state("status", "HALTED")
    db.record_error("exchange", "timeout", {"attempt": 3}, ts=NOW)
    db.close()
    db2 = Database(p)  # survives restart
    assert db2.get_state("status") == "HALTED" and db2.get_state("missing", 1) == 1
    assert len(db2.errors_between(NOW - timedelta(seconds=1), NOW + timedelta(seconds=1))) == 1
    db2.close()


def test_naive_datetime_rejected(db):
    from datetime import datetime
    with pytest.raises(ValueError):
        db.record_stoploss("btc_idr", "paper", datetime(2026, 1, 1))
