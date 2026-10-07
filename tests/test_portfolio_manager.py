"""Portfolio manager + reviewer (owner choice 2026-10-07: vol-20% allocation, weekly review)."""

import math
from datetime import timedelta
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from agent.data.market_data import PairMarket
from agent.portfolio.manager import PortfolioManager, target_weights
from agent.portfolio.portfolio import Portfolio
from agent.portfolio.review import PortfolioReviewer, format_review, shadow_return
from agent.reporting.telegram_bot import esc
from agent.storage.db import Database
from tests.helpers import NOW, book, pair_info, settings, ticker
from tests.test_strategy import feat

PAIRS = ["btc_idr", "eth_idr", "sol_idr"]


def series(n=200, drift=0.003, noise=0.02, seed=1, start=1e9):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=NOW.replace(hour=0), periods=n, freq="1D", tz="UTC")
    return pd.Series(start * np.cumprod(1 + drift + rng.normal(0, noise, n)), index=idx)


def pm_settings(**kw):
    s = settings(market={"whitelist": PAIRS}, portfolio={"enabled": True, **kw},
                 risk={"max_open_positions": 6, "max_position_pct": 45, "max_asset_exposure_pct": 45,
                       "daily_loss_limit_pct": 6, "max_drawdown_pct": 40})
    return s


def market(pair, closes: pd.Series, bid="999000000", ask="1000000000"):
    df = pd.DataFrame({"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
                       "volume": 10.0})
    return PairMarket(pair, pair_info(pair), ticker(pair), book(pair, bid=bid, ask=ask), {"1D": df},
                      NOW.timestamp())


# ------------------------------------------------------------- weights

def test_only_uptrending_coins_get_weight_and_vol_is_targeted():
    p = pm_settings().portfolio
    closes = {"btc_idr": series(seed=1), "eth_idr": series(seed=2), "sol_idr": series(drift=-0.006, seed=3)}
    w, diag = target_weights(closes, p)
    assert w["sol_idr"] == 0 and "EMA" in diag["excluded"]["sol_idr"]
    assert w["btc_idr"] > 0 and w["eth_idr"] > 0 and sum(w.values()) <= 1 + 1e-9
    r = pd.DataFrame({k: v.pct_change() for k, v in closes.items() if w[k] > 0}).dropna().tail(p.cov_lookback)
    wv = np.array([w[k] for k in r.columns])
    port = math.sqrt(wv @ (r.cov().values * 365) @ wv)
    assert port == pytest.approx(p.target_vol, rel=0.02) or diag["scale"] == 1.0


def test_calm_market_is_capped_never_levered_and_per_coin_cap():
    p = pm_settings(target_vol=1.0, max_weight=0.45).portfolio
    calm = {k: series(noise=0.001, seed=i) for i, k in enumerate(PAIRS)}
    w, diag = target_weights(calm, p)
    assert diag["scale"] <= 1.0 and all(v <= 0.45 + 1e-12 for v in w.values())


def test_short_history_and_no_trend_mean_all_cash():
    p = pm_settings().portfolio
    w, diag = target_weights({"btc_idr": series(n=50)}, p)
    assert w == {"btc_idr": 0.0} and "riwayat" in diag["excluded"]["btc_idr"]
    w, _ = target_weights({k: series(drift=-0.01, seed=i) for i, k in enumerate(PAIRS)}, p)
    assert sum(w.values()) == 0


# ------------------------------------------------------------- rebalance proposals

@pytest.fixture
def pm_env(tmp_path):
    s = pm_settings()
    db = Database(tmp_path / "pm.db")
    pm = PortfolioManager(s.portfolio, db, "paper", D(1_000_000), tuple(PAIRS))
    yield s, db, pm
    db.close()


def markets_up():
    return {"btc_idr": market("btc_idr", series(seed=1)), "eth_idr": market("eth_idr", series(seed=2)),
            "sol_idr": market("sol_idr", series(drift=-0.006, seed=3))}


def feats():
    return {k: {"1D": feat()} for k in PAIRS}


def test_first_cycle_plans_and_buys_with_catastrophe_stop(pm_env):
    s, db, pm = pm_env
    pf = Portfolio(D(1_000_000))
    res = pm.propose(markets_up(), pf, NOW, feats(), "1D", {})
    assert res.plan and pm.plan()["made_at"] == NOW.isoformat()
    buys = {p.pair: p for p in res.proposals}
    assert set(buys) == {"btc_idr", "eth_idr"} and all(p.side == "buy" for p in buys.values())
    for p in buys.values():
        assert p.stop_loss == D(1_000_000_000) - D(6) * D(str(feat().atr))   # entry - 6 x ATR, fixed
        assert p.setup == "pm_buy" and p.target > p.price


def test_sells_go_first_and_buys_wait(pm_env):
    s, db, pm = pm_env
    pf = Portfolio(D(1_000_000))
    pf.apply_fill("sol_idr", "buy", D("0.0003"), D(1_000_000_000), D(0), NOW, D(900_000_000))  # target 0
    res = pm.propose(markets_up(), pf, NOW, feats(), "1D", {})
    assert [(p.pair, p.side, p.setup) for p in res.proposals] == [("sol_idr", "sell", "pm_exit")]
    assert res.proposals[0].qty == D("0.0003")
    assert "menunggu penjualan" in res.notes["btc_idr"]


def test_plan_is_weekly_and_expires(pm_env):
    s, db, pm = pm_env
    pf = Portfolio(D(1_000_000))
    pm.propose(markets_up(), pf, NOW, feats(), "1D", {})
    made = pm.plan()["made_at"]
    pm.propose(markets_up(), pf, NOW + timedelta(days=3), feats(), "1D", {})
    assert pm.plan()["made_at"] == made                                   # no new plan mid-week
    late = pm.propose(markets_up(), pf, NOW + timedelta(days=3), feats(), "1D", {})
    assert late.proposals == [] and "menunggu rebalance" in late.notes["btc_idr"]   # plan older than 36 h
    pm.propose(markets_up(), pf, NOW + timedelta(days=7), feats(), "1D", {})
    assert pm.plan()["made_at"] != made and len(db.get_state("paper:pm:history")) == 2


def test_small_drift_is_not_traded(pm_env):
    s, db, pm = pm_env
    pf = Portfolio(D(1_000_000))
    res = pm.propose(markets_up(), pf, NOW, feats(), "1D", {})
    for p in res.proposals:   # pretend every buy filled exactly at target
        pf.apply_fill(p.pair, "buy", p.qty, D(999_000_000), D(0), NOW, p.stop_loss)
    again = pm.propose(markets_up(), pf, NOW + timedelta(hours=1), feats(), "1D", {})
    assert again.proposals == [] and all("sesuai target" in n or "tidak dipegang" in n for n in again.notes.values())


def test_missing_data_for_held_coin_defers_plan(pm_env):
    s, db, pm = pm_env
    pf = Portfolio(D(1_000_000))
    pf.apply_fill("sol_idr", "buy", D("0.0003"), D(1_000_000_000), D(0), NOW, D(900_000_000))
    mk = markets_up()
    del mk["sol_idr"]
    res = pm.propose(mk, pf, NOW, feats(), "1D", {})
    assert res.proposals == [] and pm.plan() is None


# ------------------------------------------------------------- engine + backtest integration

async def test_runner_cycle_in_portfolio_mode(tmp_path):
    from agent.reporting.telegram_bot import RecordingNotifier
    from agent.runner import AgentRunner
    from tests.test_runner import Clock

    class MD:
        async def fetch(self, pair):
            return markets_up()[pair]

    s = pm_settings()
    db = Database(tmp_path / "r.db")
    clock = Clock()
    r = AgentRunner(s, db, MD(), "paper", notifier=RecordingNotifier(), now=clock)
    r.engine.features = lambda m: {"1D": feat()}
    fills = await r.run_cycle()
    assert {f.pair for f in fills} == {"btc_idr", "eth_idr"}
    assert "Manajer portofolio" in r.portfolio_text() and "BTC" in r.portfolio_text()
    assert db.get_state("office:heartbeat")["pm"]["weights"]["sol_idr"] == 0
    text = await r.run_review(send=False)
    assert "Review manajer portofolio" in text and "AWAL" in text
    db.close()


def test_backtester_handles_rebalance_add_ons_and_partial_sells():
    from datetime import datetime, timezone

    from agent.backtest.backtester import Backtester
    s = pm_settings()
    n = 400
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    data, infos = {}, {}
    for i, pair in enumerate(PAIRS):
        rng = np.random.default_rng(i)
        c = 1e9 * np.cumprod(1 + 0.002 * np.sin(np.arange(n) / (20 + 5 * i)) + rng.normal(0, 0.02, n))
        data[pair] = {"1D": pd.DataFrame({"open": c, "high": c * 1.02, "low": c * 0.98, "close": c,
                                          "volume": 1e3}, index=idx)}
        infos[pair] = pair_info(pair)
    r = Backtester(s, data, infos, start=datetime(2024, 6, 1, tzinfo=timezone.utc)).run()
    setups = {t.exit_reason for t in r.trades}
    assert r.metrics["trades_closed"] > 0 and setups & {"pm_trim", "pm_exit", "open at end"}


# ------------------------------------------------------------- reviewer

def test_shadow_return_matches_cash_when_nothing_trends():
    p = pm_settings().portfolio
    df = pd.DataFrame({k: series(drift=-0.01, seed=i) for i, k in enumerate(PAIRS)})
    assert shadow_return(df, p, df.index[-30]) == pytest.approx(0.0)


def _reviewer_db(tmp_path, end_equities, start=NOW - timedelta(days=30)):
    db = Database(tmp_path / "rv.db")
    db.set_state("paper:pm:history", [{"made_at": start.isoformat(), "weights": {"btc_idr": 0.3},
                                       "equity": "1000000", "exposure_before": 0.3, "scale": 0.5, "port_vol": 0.4}])
    db.set_state("paper:pm:plan", {"made_at": start.isoformat(), "weights": {"btc_idr": 0.3}})
    prev = 1_000_000
    for i, e in enumerate(end_equities):
        day = (start + timedelta(days=i + 1)).date().isoformat()
        db.upsert_daily_pnl(day, "paper", start_equity=D(prev), end_equity=D(e))
        prev = e
    return db


def test_review_good_and_bad_verdicts(tmp_path):
    from zoneinfo import ZoneInfo
    p = pm_settings().portfolio
    mk = {k: market(k, series(seed=i)) for i, k in enumerate(PAIRS)}
    good = _reviewer_db(tmp_path, [1_000_000 + 500 * i for i in range(29)])
    rv = PortfolioReviewer(p, good, "paper", D(1_000_000), ZoneInfo("Asia/Jakarta"))
    rev = rv.review(NOW, mk)
    assert rev.verdict in ("BAIK", "PERHATIAN") and rev.metrics["return_pct"] > 0
    assert "shadow_return_pct" in rev.metrics and rv.last()["verdict"] == rev.verdict
    good.close()
    (tmp_path / "rv.db").unlink()
    crash = _reviewer_db(tmp_path, [1_000_000 - 15_000 * i for i in range(29)])
    rev = PortfolioReviewer(p, crash, "paper", D(1_000_000), ZoneInfo("Asia/Jakarta")).review(NOW, mk)
    assert rev.verdict == "BURUK" and rev.metrics["max_drawdown_pct"] >= 35
    text = format_review(rev, esc)
    assert "BURUK" in text and "Rekomendasi" in text and "tidak dijalankan otomatis" in text
    crash.close()


def test_review_too_early(tmp_path):
    from zoneinfo import ZoneInfo
    db = _reviewer_db(tmp_path, [1_000_000], start=NOW - timedelta(days=2))
    rev = PortfolioReviewer(pm_settings().portfolio, db, "paper", D(1_000_000),
                            ZoneInfo("Asia/Jakarta")).review(NOW, {})
    assert rev.verdict == "AWAL"
    db.close()


# ------------------------------------------------------------- reacting to changes (2026-10-07)

def shifted(m: PairMarket, closes: pd.Series) -> PairMarket:
    df = pd.DataFrame({"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes, "volume": 10.0})
    return PairMarket(m.pair, m.info, m.ticker, m.orderbook, {"1D": df}, m.fetched_at)


def test_reacts_to_a_changed_recommendation_at_the_next_daily_close(tmp_path):
    s = pm_settings(react_to_changes=True)
    db = Database(tmp_path / "re.db")
    pm = PortfolioManager(s.portfolio, db, "paper", D(1_000_000), tuple(PAIRS))
    pf = Portfolio(D(1_000_000))
    mk = markets_up()
    pm.propose(mk, pf, NOW, feats(), "1D", {})
    first = pm.plan()
    assert first["reason"] == "rencana pertama" and first["weights"]["sol_idr"] == 0
    # next day, nothing changed -> no new plan
    nxt = {k: shifted(m, m.candles["1D"]["close"].shift(-1, freq="1D")) for k, m in mk.items()}
    pm.propose(nxt, pf, NOW + timedelta(days=1), feats(), "1D", {})
    assert pm.plan()["made_at"] == first["made_at"]
    # SOL rallies above its trend line at the following close -> immediate new plan
    sol = nxt["sol_idr"].candles["1D"]["close"]
    rally = sol.copy()
    rally.iloc[-1] = sol.ewm(span=100, adjust=False).mean().iloc[-1] * 1.06   # closes just above its trend line
    nxt2 = {k: shifted(m, m.candles["1D"]["close"].shift(1, freq="1D")) for k, m in nxt.items()}
    nxt2["sol_idr"] = shifted(nxt["sol_idr"], pd.concat([rally, pd.Series([rally.iloc[-1] * 1.01],
                                                                             index=[rally.index[-1] + pd.Timedelta(days=1)])]))
    res = pm.propose(nxt2, pf, NOW + timedelta(days=2), feats(), "1D", {})
    plan = pm.plan()
    assert plan["made_at"] != first["made_at"] and plan["reason"] == "perubahan rekomendasi"
    assert any("SOL masuk tren" in c for c in plan["changes"]) and plan["weights"]["sol_idr"] > 0
    assert any(p.pair == "sol_idr" and p.side == "buy" for p in res.proposals), (res.notes, plan["weights"])
    # the same close is not re-checked again
    pm.propose(nxt2, pf, NOW + timedelta(days=2, hours=1), feats(), "1D", {})
    assert pm.plan()["made_at"] == plan["made_at"]
    db.close()


async def test_nadia_announces_plans_and_dika_sinta_flag_potentials(tmp_path):
    from agent.reporting.telegram_bot import RecordingNotifier
    from agent.runner import AgentRunner
    from tests.test_runner import Clock

    base = markets_up()
    sol = base["sol_idr"].candles["1D"]["close"]
    ema = sol.ewm(span=100, adjust=False).mean().iloc[-1]
    # SOL closed below its trend line, but the live price is now 1% above it -> potential BUY
    live = int(ema * 1.01)
    base["sol_idr"] = PairMarket("sol_idr", pair_info("sol_idr"), ticker("sol_idr"),
                                 book("sol_idr", bid=str(live), ask=str(live + 1000)), base["sol_idr"].candles,
                                 NOW.timestamp())

    class MD:
        async def fetch(self, pair):
            return base[pair]

    db = Database(tmp_path / "n.db")
    notes = RecordingNotifier()
    clock = Clock()
    r = AgentRunner(pm_settings(), db, MD(), "paper", notifier=notes, now=clock)
    r.engine.features = lambda m: {"1D": feat()}
    await r.run_cycle()
    text = "\n".join(notes.messages)
    assert "Nadia: rencana alokasi baru" in text and "rencana pertama" in text and "Instruksi ke Raka" in text
    assert "BELI" in text and "Potensi BELI SOL" in text
    n = len(notes.messages)
    clock.t += timedelta(minutes=5)
    await r.run_cycle()
    assert not any("Nadia" in m or "Potensi" in m for m in notes.messages[n:])   # no repeats
    hb = db.get_state("office:heartbeat")
    assert hb["pairs"]["sol_idr"]["pm_potential"] == "buy"
    db.close()
