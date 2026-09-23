from datetime import timedelta
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from agent.analysis.signals import compute_features
from agent.backtest.backtester import (
    BacktestConfig,
    Backtester,
    load_history,
    max_drawdown_pct,
    sharpe_daily,
)
from agent.backtest.report import verdict_text, write_report
from agent.strategy.strategy import TradeProposal
from tests.helpers import pair_info, settings


def synthetic_daily(days: int = 1500, seed: int = 7, start_price: float = 1e9,
                    start: str = "2020-01-01") -> pd.DataFrame:
    """Regime-switching random walk: alternating uptrend, flat and downtrend segments."""
    rng = np.random.default_rng(seed)
    drift = np.zeros(days)
    seg = 60
    for i in range(0, days, seg):
        drift[i:i + seg] = rng.choice([0.006, 0.0, -0.004])
    ret = drift + rng.normal(0, 0.025, days)
    close = start_price * np.cumprod(1 + ret)
    open_ = np.concatenate([[start_price], close[:-1]])
    wig = np.abs(rng.normal(0, 0.01, days))
    high = np.maximum(open_, close) * (1 + wig)
    low = np.minimum(open_, close) * (1 - wig)
    idx = pd.date_range(start, periods=days, freq="1D", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(50, 200, days)}, index=idx)


def dataset(df):
    return {"1D": df}


@pytest.fixture(scope="module")
def s1():
    return settings(market={"whitelist": ["btc_idr"]})


@pytest.fixture(scope="module")
def result(s1):
    bt = Backtester(s1, {"btc_idr": dataset(synthetic_daily())}, {"btc_idr": pair_info()})
    return bt, bt.run()


def test_run_produces_consistent_accounting(result):
    bt, r = result
    m = r.metrics
    assert len(r.equity) > 1300
    assert m["trades_closed"] > 0, m
    closed = sum((t.pnl for t in r.trades if t.pnl is not None), D(0))
    if all(t.pnl is not None for t in r.trades):
        # no open trades: net PnL must equal the sum of closed trade PnLs
        assert D(str(m["net_pnl_idr"])) == pytest.approx(closed, abs=D("0.01"))
    assert all(t.exit_time is None or t.exit_time >= t.entry_time for t in r.trades)
    assert m["pnl_before_fees_idr"] == pytest.approx(m["net_pnl_idr"] + m["total_fees_idr"])
    fees = sum((t.fees for t in r.trades), D(0))
    assert D(str(m["total_fees_idr"])) == pytest.approx(fees, abs=D("0.01"))
    assert m["total_fees_idr"] > 0


def test_hard_limits_hold_over_whole_run(result, s1):
    bt, r = result
    cap = D(str(s1.risk.agent_capital_idr))
    rows = list(bt.db._conn.execute("SELECT * FROM orders WHERE mode='backtest' ORDER BY ts_created"))
    buys = [r_ for r_ in rows if r_["side"] == "buy"]
    assert buys
    for o in buys:
        assert D(o["price"]) * D(o["qty"]) <= cap * D("0.10") + D("0.01")
        assert o["order_type"] == "limit"
    assert all(o["order_type"] == "limit" or o["is_emergency"] for o in rows)
    times = pd.to_datetime([o["ts_created"] for o in rows if not o["is_emergency"]])
    for i, t in enumerate(times):
        window = ((times > t - pd.Timedelta(hours=1)) & (times <= t)).sum()
        assert window <= s1.risk.max_orders_per_hour
    # stop-loss cooldown respected: no buy on a pair within 30 min after its stop-loss
    sls = [t for t in r.trades if t.exit_reason == "stop_loss"]
    for sl in sls:
        for o in buys:
            ts = pd.Timestamp(o["ts_created"])
            assert not (sl.exit_time < ts < sl.exit_time + timedelta(minutes=30))


def test_no_lookahead_features_match_truncated_history(result, s1):
    bt, _ = result
    df = bt.data["btc_idr"]["1D"]
    for k in (300, 700, 1200):
        t_close = int(df.index[k].timestamp()) + 86400
        f = bt._features_at("btc_idr", t_close)["1D"]
        closed = df[df.index.as_unit("s").asi8 + 86400 <= t_close]
        ref = compute_features(closed, "1D", s1.strategy)
        assert f.close == ref.close and f.regime == ref.regime
        assert f.donchian_high == pytest.approx(ref.donchian_high)
        assert f.chandelier_stop == pytest.approx(ref.chandelier_stop)
        assert f.ema_trend == pytest.approx(ref.ema_trend)


def test_forced_trade_gap_stop_loss_fills_at_open_minus_slippage(s1):
    df = synthetic_daily(days=400, seed=3)
    k = len(df) - 40
    entry_bar_close = df.index[k] + pd.Timedelta(days=1)
    c = df["close"].iloc[k]
    # entry fills at the ask at bar k's close; the next candle gaps 10% down
    df.iloc[k + 1, :4] = [c * 0.90, c * 0.91, c * 0.89, c * 0.90]
    bt = Backtester(s1, {"btc_idr": dataset(df)}, {"btc_idr": pair_info()},
                    BacktestConfig(spread_pct=0.1, slippage_pct=0.2))
    real = bt.engine.strategy.propose_entry
    fired = []

    def forced(pair, info, book, feats):
        now_close = bt.engine._now_hint
        if now_close == entry_bar_close and not fired:
            fired.append(1)
            px = book.best_ask
            return TradeProposal(pair=pair, side="buy", order_type="limit", price=px, qty=D("0.00005"),
                                 intent="entry", reason="forced", confidence=0.9,
                                 stop_loss=info.round_price(px * D("0.98"), "buy"),
                                 target=info.round_price(px * D("1.10"), "sell"), setup="forced"), "ok"
        return None, "suppressed"

    bt.engine.strategy.propose_entry = forced
    bt.engine.strategy.propose_exit = lambda *a, **k: None  # isolate the intrabar stop-loss path
    bt.engine.strategy.trail_stop = lambda *a, **k: None
    orig_run = bt.engine.run_cycle

    async def run_cycle(markets, pf, now, status, **kw):
        bt.engine._now_hint = pd.Timestamp(now)
        return await orig_run(markets, pf, now, status, **kw)

    bt.engine.run_cycle = run_cycle
    r = bt.run()
    assert real is not None and fired
    forced_trades = [t for t in r.trades if t.setup == "forced"]
    assert len(forced_trades) == 1
    t = forced_trades[0]
    assert t.exit_reason == "stop_loss"
    gap_open = D(repr(float(c * 0.90)))
    assert t.exit_price <= gap_open * (1 - D("0.002")) + 1000  # at the gap open, minus slippage, not at SL
    assert t.exit_price < t.entry_price * D("0.98")
    assert t.pnl < 0
    assert bt.db.last_stoploss_times("backtest")["btc_idr"] is not None


def test_report_files(result, tmp_path):
    _, r = result
    p = write_report(r, tmp_path, "t")
    assert p.exists() and (tmp_path / "trades.csv").exists() and (tmp_path / "equity.csv").exists()
    assert "PnL sebelum fee" in p.read_text()


def test_verdict_text_is_honest():
    base = {"trades_closed": 5}
    assert "POSITIF" in verdict_text({**base, "net_pnl_idr": 10, "pnl_before_fees_idr": 20})
    assert "fee memakan" in verdict_text({**base, "net_pnl_idr": -10, "pnl_before_fees_idr": 5})
    assert "bahkan sebelum fee" in verdict_text({**base, "net_pnl_idr": -10, "pnl_before_fees_idr": -5})
    assert "Tidak ada trade" in verdict_text({"trades_closed": 0, "net_pnl_idr": 0, "pnl_before_fees_idr": 0})


def test_metric_helpers():
    idx = pd.date_range("2026-01-01", periods=5, freq="1D", tz="UTC")
    eq = pd.Series([100, 120, 90, 95, 130], index=idx, dtype=float)
    assert max_drawdown_pct(eq) == pytest.approx(25.0)
    assert sharpe_daily(pd.Series([100.0] * 5, index=idx)) == 0.0
    assert sharpe_daily(eq) != 0


def test_load_history_roundtrip(tmp_path):
    df = synthetic_daily(days=10)
    out = df.copy()
    out.insert(0, "ts", out.index.as_unit("s").asi8)
    out.to_csv(tmp_path / "btc_idr_1D.csv", index=False)
    back = load_history(tmp_path, "btc_idr", "1D")
    assert back.index.equals(df.index) and np.allclose(back["close"], df["close"])


def test_missing_pair_info_rejected(s1):
    with pytest.raises(ValueError):
        Backtester(s1, {"btc_idr": dataset(synthetic_daily(days=200))}, {})


def test_not_enough_history_rejected(s1):
    with pytest.raises(ValueError):
        Backtester(s1, {"btc_idr": dataset(synthetic_daily(days=50))}, {"btc_idr": pair_info()}).run()


def test_unclosed_candle_is_dropped(s1):
    today = pd.Timestamp.now(tz="UTC").normalize()
    df = synthetic_daily(days=300, start=str((today - pd.Timedelta(days=299)).date()))
    assert df.index[-1] == today                     # today's candle is still forming
    bt = Backtester(s1, {"btc_idr": dataset(df)}, {"btc_idr": pair_info()})
    assert bt.data["btc_idr"]["1D"].index[-1] == today - pd.Timedelta(days=1)


def test_pairs_join_timeline_when_their_data_starts():
    s = settings(market={"whitelist": ["btc_idr", "eth_idr"]})
    btc = synthetic_daily(days=1000, seed=1)
    eth = synthetic_daily(days=500, seed=2, start="2021-05-15")
    bt = Backtester(s, {"btc_idr": dataset(btc), "eth_idr": dataset(eth)},
                    {"btc_idr": pair_info(), "eth_idr": pair_info("eth_idr")})
    r = bt.run()
    assert r.start < eth.index[0].to_pydatetime()      # timeline started before ETH existed
    eth_trades = [t for t in r.trades if t.pair == "eth_idr"]
    assert all(t.entry_time >= eth.index[0].to_pydatetime() for t in eth_trades)
