"""scripts/paper_report.py on a database shaped like the owner's paper run (no network)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from agent.risk.risk_manager import RiskDecision, Verdict
from agent.storage.db import Database
from scripts.paper_report import build_report, closed_trades, equity_curve, load_run, max_drawdown_pct
from tests.helpers import buy

T0 = datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)
WIB = ZoneInfo("Asia/Jakarta")


def make_db(path):
    db = Database(path)
    # 500k capital, later lowered to 300k on 10-04 (start rebased by -200k, as the runner does)
    for i in range(12):
        day = (T0 + timedelta(days=i)).date().isoformat()
        start = D(500_000) if i <= 3 else D(499_862)
        end = D(500_000) if i < 3 else D(499_862)       # -138 fee on day 3 only
        if i == 11:
            start, end = start - 200_000, end - 200_000
        db.upsert_daily_pnl(day, "paper", start_equity=start, end_equity=end, unrealized=D(-138) if i >= 3 else None)
    db.set_state("paper:capital", "300000.0")
    db.record_fill(trade_id="a:1", client_order_id="pa-1", ts=T0 + timedelta(days=3), mode="paper", pair="sol_idr",
                   side="buy", price=D(2_173_981), qty=D("0.01857527"), fee_idr=D(138))
    db.set_state("paper:stops", {"sol_idr": "1966129"})
    db.record_fill(trade_id="b:1", client_order_id="pb-1", ts=T0 + timedelta(days=4), mode="paper", pair="eth_idr",
                   side="buy", price=D(48_000_000), qty=D("0.001"), fee_idr=D(164))
    db.record_fill(trade_id="b:2", client_order_id="pb-2", ts=T0 + timedelta(days=6), mode="paper", pair="eth_idr",
                   side="sell", price=D(46_000_000), qty=D("0.001"), fee_idr=D(157), realized_pnl=D(-2321))
    db.record_stoploss("eth_idr", "paper", T0 + timedelta(days=6))
    p = buy()
    db.record_decision(RiskDecision(Verdict.VETO, p, D(0), p.price, ("spread 0.41% > max 0.3%",)), "paper", ts=T0)
    db.record_decision(RiskDecision(Verdict.VETO, p, D(0), p.price, ("spread 0.52% > max 0.3%",)), "paper", ts=T0)
    db.record_error("exchange", "timeout", ts=T0 + timedelta(days=1))
    db.close()


def test_report_from_owner_shaped_db(tmp_path):
    path = tmp_path / "agent.db"
    make_db(path)
    run = load_run(str(path), "paper", D(500_000))
    assert run.capital == D(300_000) and run.capital_first == D(500_000)
    assert set(run.portfolio.positions) == {"sol_idr"}
    assert run.portfolio.positions["sol_idr"].stop_loss == D(1_966_129)
    run.marks = {"sol_idr": D(2_174_006)}
    md, tg = build_report(run, WIB, T0 + timedelta(days=12))
    assert "12 hari tercatat" in md and "diubah pemilik" in md
    assert "trade selesai 1 (menang 0, kalah 1)" in md and "stop-loss kena 1" in md
    assert "2× spread #% > max #%" in md                     # veto reasons grouped
    assert "exchange: 1×" in md and "Rp 1.966.129" in md
    assert "Laporan akhir paper trading" in tg and "<b>" in tg


def test_capital_change_does_not_show_as_drawdown(tmp_path):
    path = tmp_path / "agent.db"
    make_db(path)
    run = load_run(str(path), "paper", D(500_000))
    curve = equity_curve(run)
    assert min(eq for _, eq in curve) > D(299_000)          # no 500k -> 300k cliff
    assert max_drawdown_pct(curve) < D("0.1")


def test_closed_trades_round_trip():
    rows = [{"pair": "x", "side": "buy", "qty": "1", "fee_idr": "1", "ts": "a", "realized_pnl": None},
            {"pair": "x", "side": "sell", "qty": "0.5", "fee_idr": "1", "ts": "b", "realized_pnl": "5"},
            {"pair": "x", "side": "sell", "qty": "0.5", "fee_idr": "1", "ts": "c", "realized_pnl": "-2"}]
    t = closed_trades(rows)
    assert len(t) == 1 and t[0]["pnl"] == 3 and t[0]["fees"] == 3
