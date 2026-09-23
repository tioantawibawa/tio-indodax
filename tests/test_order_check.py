"""Sizing of scripts/live_order_check.py (no network)."""

from decimal import Decimal as D

from scripts.live_order_check import HARD_CAP_IDR, min_qty_at, plan_orders
from tests.helpers import pair_info


def test_min_qty_uses_the_order_price_not_the_ask():
    # real rejection: limit at 0.9 x bid 1,510,001,000 -> "Minimum order is 0.00000735 BTC"
    info = pair_info(min_base="0", step="0.00000001", tick="1000")
    price = info.round_price(D("1510001000") * D("0.90"), "buy")
    qty = min_qty_at(info, price, margin=D(1))
    assert qty >= D("0.00000735")
    assert info.min_order_violation(price, qty) is None


def test_plan_meets_minimum_on_every_leg_and_stays_under_cap():
    info = pair_info(min_base="0")
    bid, ask = D("1510001000"), D("1510040000")
    p = plan_orders(info, bid, ask)
    assert info.min_order_violation(p["a_price"], p["a_qty"]) is None
    assert info.min_order_violation(p["b_buy_price"], p["b_qty"]) is None
    # sell leg still meets the minimum even if ~10% of the coin went to fees/rounding
    assert info.min_order_violation(p["b_sell_price"], info.round_qty(p["b_qty"] * D("0.90"))) is None
    assert p["a_price"] * p["a_qty"] < HARD_CAP_IDR
    assert p["b_buy_price"] * p["b_qty"] < HARD_CAP_IDR
    assert p["a_price"] < bid and p["b_buy_price"] > ask and p["b_sell_price"] < bid


def test_min_base_respected():
    info = pair_info(min_base="0.001", min_quote="10000")
    assert min_qty_at(info, D("1000000000")) == D("0.001")
