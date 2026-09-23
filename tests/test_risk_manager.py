"""Every hard limit has at least one test proving it cannot be violated.

Baseline: capital Rp 1.000.000, BTC ~Rp 1 miliar, 10% max position = Rp 100.000.
"""

import random
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from agent.risk.risk_manager import AgentStatus, RiskManager, Verdict
from tests.helpers import NOW, D, book, buy, ctx, market_view, sell, settings, with_position


@pytest.fixture
def s():
    return settings()


@pytest.fixture
def rm(s):
    return RiskManager(s.risk, s.market, s.fees)


def test_baseline_buy_approved(rm):
    d = rm.evaluate(buy(), ctx(), market_view())
    assert d.verdict == Verdict.APPROVE, d.reasons
    assert d.qty == D("0.0001") and d.price == D("999000000")


# ------------------------------------------------------------ immutability

def test_limits_cannot_be_changed_at_runtime(rm):
    with pytest.raises(ValidationError):
        rm.limits.max_position_pct = 90  # type: ignore[misc]
    with pytest.raises(AttributeError):
        rm.limits = None  # type: ignore[misc]  # read-only property
    assert rm.limits.max_position_pct == 10


def test_proposal_confidence_cannot_override_limits(rm):
    p = replace(buy(qty="0.01"), confidence=1.0)  # Rp 10 jt, 1000% of the position cap
    d = rm.evaluate(p, ctx(), market_view())
    assert d.qty * d.price <= D(100_000)


# ------------------------------------------------------------ whitelist

def test_non_whitelisted_pair_vetoed(rm):
    d = rm.evaluate(buy(pair="doge_idr"), ctx(), market_view("doge_idr"))
    assert d.verdict == Verdict.VETO and "whitelist" in d.reasons[0]


def test_market_data_pair_mismatch_vetoed(rm):
    d = rm.evaluate(buy(pair="eth_idr"), ctx(), market_view("btc_idr"))
    assert d.verdict == Verdict.VETO


def test_suspended_pair_vetoed(rm):
    mv = market_view()
    mv = replace(mv, info=replace(mv.info, is_suspended=True))
    assert rm.evaluate(buy(), ctx(), mv).verdict == Verdict.VETO


# ------------------------------------------------------------ max position %

def test_max_position_resizes(rm):
    d = rm.evaluate(buy(qty="0.0005"), ctx(), market_view())  # Rp ~500k
    assert d.verdict == Verdict.RESIZE
    assert d.qty * d.price <= D(100_000)
    assert d.qty > 0


def test_max_position_counts_existing_position(rm):
    c = with_position(ctx(cash_idr=D(920_000)), "btc_idr", "0.00008", "80000")
    d = rm.evaluate(buy(qty="0.0001"), c, market_view())
    assert d.verdict == Verdict.RESIZE
    assert d.qty * d.price <= D(20_000)


def test_max_position_full_vetoes(rm):
    c = with_position(ctx(cash_idr=D(900_000)), "btc_idr", "0.0001", "100000")
    d = rm.evaluate(buy(), c, market_view())
    assert d.verdict == Verdict.VETO and "max_position" in d.reasons[0]


def test_max_position_counts_pending_buys(rm):
    d = rm.evaluate(buy(), ctx(pending_buy_idr={"btc_idr": D(95_000)}), market_view())
    assert d.verdict == Verdict.VETO  # only Rp 5.000 room < Rp 10.000 exchange minimum


# ------------------------------------------------------------ max open positions

def test_max_open_positions_blocks_new_pair(rm):
    c = ctx(cash_idr=D(700_000))
    for p in ("eth_idr", "sol_idr", "xrp_idr"):
        c = with_position(c, p, "1", "100000")
    d = rm.evaluate(buy(), c, market_view())
    assert d.verdict == Verdict.VETO and "max open positions" in d.reasons[0]


def test_pending_buy_counts_as_open_position(rm):
    c = with_position(with_position(ctx(), "eth_idr", "1", "50000"), "sol_idr", "1", "50000")
    c = replace(c, pending_buy_idr={"xrp_idr": D(50_000)})
    assert rm.evaluate(buy(), c, market_view()).verdict == Verdict.VETO


def test_adding_to_existing_pair_not_blocked_by_position_count():
    s = settings(risk={"max_position_pct": 20})
    rm = RiskManager(s.risk, s.market, s.fees)
    c = ctx()
    for p in ("eth_idr", "sol_idr", "btc_idr"):
        c = with_position(c, p, "0.00005", "50000")
    assert rm.evaluate(buy(), c, market_view()).approved


# ------------------------------------------------------------ asset exposure

def test_asset_exposure_across_pairs():
    s = settings(risk={"max_position_pct": 10, "max_asset_exposure_pct": 15})
    rm = RiskManager(s.risk, s.market, s.fees)
    c = with_position(ctx(), "btc_usdt", "0.0001", "100000")  # same asset, other market
    d = rm.evaluate(buy(qty="0.0001"), c, market_view())
    assert d.verdict == Verdict.RESIZE
    assert d.qty * d.price <= D(50_000)
    assert "max_asset_exposure" in d.reasons[0]


# ------------------------------------------------------------ agent capital / cash

def test_cannot_spend_more_than_agent_cash(rm):
    d = rm.evaluate(buy(), ctx(cash_idr=D(50_000)), market_view())
    assert d.verdict == Verdict.RESIZE
    fee = D("1.003422")  # taker 0.2% + tax 0.12% + clearing 0.0222% (worst case)
    assert d.qty * d.price * fee <= D(50_000)


def test_cannot_exceed_agent_capital_even_if_cash_ledger_is_wrong(rm):
    c = ctx(cash_idr=D(5_000_000))  # corrupted ledger claims more cash than the allocation
    for p in ("eth_idr", "sol_idr"):
        c = with_position(c, p, "1", "480000")
    d = rm.evaluate(buy(), c, market_view())
    assert d.qty * d.price <= D(40_000)


def test_exchange_free_balance_caps(rm):
    d = rm.evaluate(buy(), ctx(exchange_free_idr=D(30_000)), market_view())
    assert d.verdict == Verdict.RESIZE and d.qty * d.price <= D(30_000)


def test_no_cash_vetoes(rm):
    assert rm.evaluate(buy(), ctx(cash_idr=D(0)), market_view()).verdict == Verdict.VETO


# ------------------------------------------------------------ daily loss limit

def test_daily_loss_limit_blocks_entries_and_flags(rm):
    c = ctx(equity_idr=D(969_000), day_start_equity_idr=D(1_000_000))
    d = rm.evaluate(buy(), c, market_view())
    assert d.verdict == Verdict.VETO and d.trigger_daily_stop
    assert "daily loss" in " ".join(d.reasons)


def test_daily_loss_just_below_limit_allows(rm):
    c = ctx(equity_idr=D(971_000), day_start_equity_idr=D(1_000_000))
    assert rm.evaluate(buy(), c, market_view()).approved


def test_daily_loss_still_allows_exits(rm):
    c = with_position(ctx(equity_idr=D(960_000)), "btc_idr", "0.0001", "99900")
    d = rm.evaluate(sell(), c, market_view())
    assert d.approved and d.trigger_daily_stop


# ------------------------------------------------------------ drawdown kill switch

def test_drawdown_kill_switch_blocks_and_triggers_halt(rm):
    c = ctx(equity_idr=D(1_000_000), peak_equity_idr=D(1_200_000))  # 16.7% DD
    d = rm.evaluate(buy(), c, market_view())
    assert d.verdict == Verdict.VETO and d.trigger_halt
    halt, _, _ = rm.check_account(c)
    assert halt


def test_drawdown_below_threshold_ok(rm):
    c = ctx(equity_idr=D(1_000_000), peak_equity_idr=D(1_170_000), day_start_equity_idr=D(1_000_000))
    halt, _, _ = rm.check_account(c)
    assert not halt


def test_halted_blocks_everything_except_emergency_exit(rm):
    c = with_position(ctx(status=AgentStatus.HALTED), "btc_idr", "0.0001", "99900")
    assert rm.evaluate(buy(), c, market_view()).verdict == Verdict.VETO
    assert rm.evaluate(sell(), c, market_view()).verdict == Verdict.VETO
    assert rm.evaluate(sell(emergency=True), c, market_view()).approved


def test_paused_blocks_entries_allows_exits(rm):
    c = with_position(ctx(status=AgentStatus.PAUSED), "btc_idr", "0.0001", "99900")
    assert rm.evaluate(buy(), c, market_view()).verdict == Verdict.VETO
    assert rm.evaluate(sell(), c, market_view()).approved


# ------------------------------------------------------------ order rate limits

@pytest.mark.parametrize("field,value", [("orders_last_hour", 10), ("orders_today", 40)])
def test_order_rate_limits(rm, field, value):
    c = with_position(ctx(**{field: value}), "btc_idr", "0.0001", "99900")
    assert rm.evaluate(buy(), c, market_view()).verdict == Verdict.VETO
    assert rm.evaluate(sell(), c, market_view()).verdict == Verdict.VETO
    # emergency stop-loss exits are exempt (blocking a stop-loss increases risk)
    assert rm.evaluate(sell(emergency=True), c, market_view()).approved


def test_order_rate_just_below_limit_ok(rm):
    assert rm.evaluate(buy(), ctx(orders_last_hour=9, orders_today=39), market_view()).approved


# ------------------------------------------------------------ limit orders only

def test_market_buy_always_vetoed(rm):
    d = rm.evaluate(buy(order_type="market"), ctx(), market_view())
    assert d.verdict == Verdict.VETO and "market orders" in d.reasons[0]


def test_market_sell_without_emergency_vetoed(rm):
    c = with_position(ctx(), "btc_idr", "0.0001", "99900")
    assert rm.evaluate(sell(order_type="market"), c, market_view()).verdict == Verdict.VETO


def test_emergency_flag_on_buy_rejected(rm):
    assert rm.evaluate(buy(is_emergency_exit=True), ctx(), market_view()).verdict == Verdict.VETO


def test_unknown_order_type_vetoed(rm):
    assert rm.evaluate(buy(order_type="stop_limit"), ctx(), market_view()).verdict == Verdict.VETO


def test_emergency_exit_slippage_cap(rm):
    c = with_position(ctx(), "btc_idr", "0.001", "999000")
    thin = book(depth_qty="0.0002", levels=5, tick="10000000")  # levels 1% apart
    d = rm.evaluate(sell(qty="0.001", emergency=True), c, market_view(book=thin))
    assert d.verdict == Verdict.VETO and "slippage" in d.reasons[0]
    assert rm.evaluate(sell(qty="0.001", emergency=True), c, market_view()).approved


def test_emergency_exit_empty_book_vetoed(rm):
    c = with_position(ctx(), "btc_idr", "0.0001", "99900")
    empty = replace(book(), bids=())
    assert rm.evaluate(sell(emergency=True), c, market_view(book=empty)).verdict == Verdict.VETO


# ------------------------------------------------------------ stop-loss mandatory

def test_stop_loss_mandatory(rm):
    assert rm.evaluate(buy(sl=None), ctx(), market_view()).verdict == Verdict.VETO
    assert rm.evaluate(buy(sl="0"), ctx(), market_view()).verdict == Verdict.VETO


def test_stop_loss_must_be_below_entry(rm):
    assert rm.evaluate(buy(sl="999000000"), ctx(), market_view()).verdict == Verdict.VETO


def test_take_profit_required_above_entry(rm):
    assert rm.evaluate(buy(tp=None), ctx(), market_view()).verdict == Verdict.VETO
    assert rm.evaluate(buy(tp="990000000"), ctx(), market_view()).verdict == Verdict.VETO


# ------------------------------------------------------------ cooldown

def test_cooldown_after_stop_loss(rm):
    c = ctx(last_stoploss_at={"btc_idr": NOW - timedelta(minutes=10)})
    d = rm.evaluate(buy(), c, market_view())
    assert d.verdict == Verdict.VETO and "cooldown" in d.reasons[0]
    c = ctx(last_stoploss_at={"btc_idr": NOW - timedelta(minutes=31)})
    assert rm.evaluate(buy(), c, market_view()).approved


def test_cooldown_is_per_pair(rm):
    c = ctx(last_stoploss_at={"eth_idr": NOW - timedelta(minutes=1)})
    assert rm.evaluate(buy(), c, market_view()).approved


# ------------------------------------------------------------ reconciliation

def test_reconciliation_mismatch_blocks_all_orders(rm):
    c = with_position(ctx(reconciliation_ok=False), "btc_idr", "0.0001", "99900")
    for p in (buy(), sell(), sell(emergency=True)):
        d = rm.evaluate(p, c, market_view())
        assert d.verdict == Verdict.VETO and "reconcile" in d.reasons[0]


# ------------------------------------------------------------ costs & liquidity

def test_expected_move_must_cover_twice_costs(rm):
    d = rm.evaluate(buy(tp="1005000000"), ctx(), market_view())  # +0.6% vs ~0.68% cost
    assert d.verdict == Verdict.VETO and "round-trip cost" in d.reasons[0]
    assert float(d.metrics["round_trip_cost_pct"]) == pytest.approx(0.6845, abs=0.01)


def test_wide_spread_vetoed(rm):
    wide = book(bid="990000000", ask="1000000000")  # ~1% spread
    d = rm.evaluate(buy(price="990000000", sl="970000000"), ctx(), market_view(book=wide))
    assert d.verdict == Verdict.VETO and "spread" in d.reasons[0]


def test_low_volume_vetoed(rm):
    d = rm.evaluate(buy(), ctx(), market_view(ticker={"vol_quote": "100000000"}))
    assert d.verdict == Verdict.VETO and "volume" in d.reasons[0]


def test_thin_book_vetoed(rm):
    thin = book(depth_qty="0.00002", levels=2)
    assert rm.evaluate(buy(), ctx(), market_view(book=thin)).verdict == Verdict.VETO


def test_buy_price_far_above_ask_vetoed(rm):
    d = rm.evaluate(buy(price="1010000000", tp="1060000000", sl="990000000"), ctx(), market_view())
    assert d.verdict == Verdict.VETO and "above best ask" in d.reasons[0]


# ------------------------------------------------------------ sells / no shorting

def test_sell_without_position_vetoed(rm):
    d = rm.evaluate(sell(), ctx(), market_view())
    assert d.verdict == Verdict.VETO and "shorting" in d.reasons[0]


def test_sell_more_than_held_resized(rm):
    c = with_position(ctx(), "btc_idr", "0.00005", "49950")
    d = rm.evaluate(sell(qty="0.0001"), c, market_view())
    assert d.verdict == Verdict.RESIZE and d.qty == D("0.00005")


def test_sell_below_exchange_minimum_vetoed(rm):
    c = with_position(ctx(), "btc_idr", "0.000005", "4995")
    assert rm.evaluate(sell(qty="0.000005"), c, market_view()).verdict == Verdict.VETO


def test_resize_below_exchange_minimum_vetoed(rm):
    d = rm.evaluate(buy(), ctx(cash_idr=D(8_000)), market_view())
    assert d.verdict == Verdict.VETO and "minimum" in d.reasons[0]


def test_qty_rounded_to_exchange_step(rm):
    d = rm.evaluate(buy(qty="0.000099999999"), ctx(), market_view())
    assert d.qty == D("0.00009999")


# ------------------------------------------------------------ fuzz / invariants

def test_invariants_hold_for_random_inputs(rm, s):
    rng = random.Random(42)
    cap = D(str(s.risk.agent_capital_idr))
    seen = {"buy": 0, "sell": 0, "resize": 0, "emergency": 0}
    for _ in range(3000):
        held_btc = D(rng.choice(["0", "0.00005", "0.0001", "0.0003"]))
        equity = D(rng.randint(800_000, 1_200_000))
        c = ctx(
            status=AgentStatus.RUNNING if rng.random() < 0.8 else rng.choice(list(AgentStatus)),
            reconciliation_ok=rng.random() > 0.05,
            equity_idr=equity,
            peak_equity_idr=equity * D(str(1 + rng.choice([0, 0, 0.05, 0.14, 0.2]))),
            day_start_equity_idr=equity * D(str(1 + rng.choice([-0.02, 0, 0.01, 0.029, 0.05]))),
            cash_idr=D(rng.choice([0, 20_000, 300_000, 1_000_000, 1_500_000])),
            orders_last_hour=rng.choice([0, 3, 9, 10, 12]),
            orders_today=rng.choice([0, 20, 39, 40]),
            last_stoploss_at=rng.choice([{}, {}, {"btc_idr": NOW - timedelta(minutes=rng.randint(0, 60))}]),
            pending_buy_idr=rng.choice([{}, {}, {"eth_idr": D(50_000)}]),
            exchange_free_idr=rng.choice([None, None, D(rng.randint(0, 2_000_000))]),
        )
        if held_btc > 0:
            c = with_position(c, "btc_idr", str(held_btc), str(held_btc * D(999_000_000)))
        for _k in range(rng.choice([0, 0, 1, 3])):
            c = with_position(c, rng.choice(["eth_idr", "sol_idr", "xrp_idr"]), "1", str(rng.randint(10_000, 200_000)))
        if rng.random() < 0.6:
            p = buy(qty=str(D(rng.randint(1, 100_000)) / D(10**8)),
                    order_type=rng.choice(["limit", "limit", "market"]),
                    sl=rng.choice([None, "979000000", "979000000", "999500000"]),
                    tp=rng.choice([None, "1040000000", "1040000000", "1001000000"]))
        else:
            p = sell(qty=str(D(rng.randint(1, 50_000)) / D(10**8)), emergency=rng.random() < 0.3,
                     order_type=rng.choice([None, "limit", "market"]))
        d = rm.evaluate(p, c, market_view())
        if not d.approved:
            assert d.qty == 0
            continue
        notional = d.qty * d.price
        seen[p.side] += 1
        seen["resize"] += d.verdict == Verdict.RESIZE
        seen["emergency"] += p.is_emergency_exit
        assert c.reconciliation_ok
        if p.side == "buy":
            dd = (c.peak_equity_idr - c.equity_idr) / c.peak_equity_idr * 100
            dl = (c.day_start_equity_idr - c.equity_idr) / cap * 100
            assert c.status == AgentStatus.RUNNING
            assert p.order_type == "limit"
            assert dd < 15 and dl < 3
            assert c.orders_last_hour < 10 and c.orders_today < 40
            assert p.stop_loss is not None and p.stop_loss < d.price
            assert "btc_idr" not in c.last_stoploss_at or NOW - c.last_stoploss_at["btc_idr"] >= timedelta(minutes=30)
            pos_after = c.position_value_idr.get("btc_idr", D(0)) + notional
            assert pos_after <= cap * D("0.10") + D("0.01")
            open_pairs = {k for k, q in c.position_qty.items() if q > 0} | set(c.pending_buy_idr)
            assert "btc_idr" in open_pairs or len(open_pairs) < 3
            assert notional <= c.cash_idr
            if c.exchange_free_idr is not None:
                assert notional <= c.exchange_free_idr
            assert notional >= D(10_000)
        else:
            assert d.qty <= c.position_qty.get("btc_idr", D(0))
            if p.order_type == "market":
                assert p.is_emergency_exit
            if c.status == AgentStatus.HALTED:
                assert p.is_emergency_exit
            if not p.is_emergency_exit:
                assert c.orders_last_hour < 10 and c.orders_today < 40
    # the fuzz must actually exercise approvals, not only vetoes
    assert all(v >= 20 for v in seen.values()), seen
