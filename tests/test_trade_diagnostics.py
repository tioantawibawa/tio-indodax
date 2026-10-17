"""scripts/trade_diagnostics.py on a database produced by the real runner (portfolio mode)."""

from datetime import datetime, timezone
from decimal import Decimal as D

from agent.risk.risk_manager import RiskDecision, Verdict
from agent.storage.db import Database
from scripts.trade_diagnostics import explain, main, norm
from tests.helpers import buy
from tests.test_portfolio_manager import markets_up, pm_settings
from tests.test_strategy import feat


def test_explanations_cover_common_vetoes():
    assert "minimum" in explain("position too small to exit at its stop-loss: 9 < 1.2 x exchange minimum 10000")
    assert "volume" in explain("24h volume 500000000 IDR < min 1000000000")
    assert norm("qty 0.00001 < minimum 0.0001 btc") == "qty # < minimum # btc"


async def test_report_shows_funnel_vetoes_unfilled_orders_and_pm_targets(tmp_path, capsys):
    from agent.reporting.telegram_bot import RecordingNotifier
    from agent.runner import AgentRunner

    class MD:
        async def fetch(self, pair):
            return markets_up()[pair]

    now = datetime.now(timezone.utc)
    db = Database(tmp_path / "d.db")
    r = AgentRunner(pm_settings(), db, MD(), "paper", notifier=RecordingNotifier(), now=lambda: now)
    r.engine.features = lambda m: {"1D": feat()}
    await r.run_cycle()
    p = buy(pair="eth_idr")
    db.record_decision(RiskDecision(Verdict.VETO, p, D(0), p.price, ("24h volume 5 IDR < min 1000000000",)),
                       "paper", ts=now)
    db.record_order(client_order_id="ag1-99", decision_id=None, mode="paper", pair="eth_idr", side="buy",
                    order_type="limit", price=D(1), qty=D(1), status="CANCELLED", is_emergency=False, ts=now)
    db.record_error("executor", "order rejected ag1-98: insufficient balance", ts=now)
    db.close()
    assert main(["--db", str(tmp_path / "d.db")]) == 0
    out = capsys.readouterr().out
    for part in ("Corong keputusan", "Proposal", "di-veto 1", "volume 24 jam", "Order yang tidak terisi",
                 "CANCELLED", "Manajer portofolio — target vs dipegang", "BTC: target", "dipegang", "Error"):
        assert part in out, part
