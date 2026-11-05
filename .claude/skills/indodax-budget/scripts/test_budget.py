#!/usr/bin/env python3
"""Offline tests for budget.py (standard library only):  python3 test_budget.py"""

import io
import json
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import budget  # noqa: E402

PX = budget.manual_prices({"btc": D("1000000"), "eth": D("50000")})
CFG = dict(budget.DEFAULTS)


class SizeBuyTests(unittest.TestCase):
    def test_cap_binds_on_empty_account(self):
        d = budget.size("btc", "buy", D(300000), {}, PX, CFG)
        self.assertEqual((d["order_idr"], d["limited_by"]), (D(75000), "coin_cap"))       # 25% of 300k
        self.assertEqual(d["reserve_idr"], D(60000))
        self.assertEqual(d["est_amount"], (D(75000) * (1 - D("0.003422")) / D(1000000)).quantize(D("1e-8"), "ROUND_DOWN"))

    def test_existing_position_reduces_headroom(self):
        # equity 300k (200k cash + 0.1 btc = 100k) → cap 75k already exceeded
        d = budget.size("btc", "buy", D(200000), {"btc": D("0.1")}, PX, CFG)
        self.assertFalse(d["ok"])
        self.assertEqual(d["order_idr"], 0)
        self.assertIn("minimum", d["reason"])

    def test_cash_buffer_binds(self):
        # equity 300k, cash 70k, reserve 60k → only 10k deployable
        d = budget.size("eth", "buy", D(70000), {"btc": D("0.23")}, PX, CFG)
        self.assertEqual((d["order_idr"], d["limited_by"]), (D(10000), "deployable"))

    def test_requested_and_pct(self):
        self.assertEqual(budget.size("btc", "buy", D(300000), {}, PX, CFG, want_idr=D(20000))["order_idr"], 20000)
        self.assertEqual(budget.size("btc", "buy", D(300000), {}, PX, CFG, want_pct=D(10))["order_idr"], 30000)

    def test_risk_based_sizing(self):
        # 1% of 1,000,000 = 10k at risk; stop 5% + 2×0.3422% fees → 10000/0.056844
        cfg = {**CFG, "max_coin_pct": D(100), "cash_buffer_pct": D(0)}
        d = budget.size("btc", "buy", D(1000000), {}, PX, cfg, stop_pct=D(5))
        self.assertEqual(d["limited_by"], "risk_per_trade")
        self.assertEqual(d["order_idr"], int(D(10000) / D("0.056844")))

    def test_below_minimum_is_zero(self):
        d = budget.size("btc", "buy", D(300000), {}, PX, CFG, want_idr=D(5000))
        self.assertEqual((d["ok"], d["order_idr"], d["est_amount"]), (False, 0, 0))


class SizeSellTests(unittest.TestCase):
    def test_sell_pct_of_holding(self):
        d = budget.size("btc", "sell", D(0), {"btc": D("0.1")}, PX, CFG, want_pct=D(50))
        self.assertEqual((d["est_amount"], d["order_idr"]), (D("0.05"), D(50000)))

    def test_sell_without_holding(self):
        self.assertFalse(budget.size("eth", "sell", D(100000), {}, PX, CFG)["ok"])


class ParseTests(unittest.TestCase):
    def test_balance_formats(self):
        self.assertEqual(budget.parse_balance({"idr": "1000", "holdings": {"BTC": "0.1", "eth": "0"}}),
                         (D(1000), {"btc": D("0.1")}))
        self.assertEqual(budget.parse_balance({"balance": {"idr": "5", "btc": "0.2"}}), (D(5), {"btc": D("0.2")}))
        for bad in ({}, {"idr": "-1"}, {"idr": "x"}, []):
            with self.assertRaises(budget.InputError):
                budget.parse_balance(bad)

    def test_pairs(self):
        self.assertEqual(budget.coin_of("BTC/IDR"), "btc")
        for bad in ("usdt_btc", "idr", "", "btc;idr"):
            with self.assertRaises(budget.InputError):
                budget.coin_of(bad)

    def test_missing_price(self):
        with self.assertRaises(budget.InputError):
            budget.size("btc", "buy", D(1000), {"sol": D(1)}, PX, CFG)


class PlanTests(unittest.TestCase):
    def test_plan_headroom_and_over_cap(self):
        d = budget.plan(D(200000), {"btc": D("0.1")}, PX, CFG, {"eth"})
        rows = {r["coin"]: r for r in d["coins"]}
        self.assertTrue(rows["btc"]["over_cap"])
        self.assertEqual(rows["btc"]["headroom_idr"], 0)
        self.assertEqual(rows["eth"]["headroom_idr"], 75000)
        self.assertEqual(d["deployable_idr"], 140000)


class CliTests(unittest.TestCase):
    def run_cli(self, argv, stdin=""):
        with mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            return budget.main(argv), out.getvalue()

    def test_stdin_balance_with_manual_price(self):
        rc, out = self.run_cli(["size", "btc_idr", "--balance", "-", "--price", "btc=1000000"],
                               json.dumps({"idr": 300000}))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["order_idr"], "75000")

    def test_bad_inputs_exit_2(self):
        for argv in (["size", "btc_idr"], ["size", "btc_idr", "--idr", "1000", "--price", "btc=0"],
                     ["size", "btc_idr", "--idr", "1000", "--price", "btc=1", "--pct", "150"],
                     ["size", "btc_idr", "--idr", "1000", "--price", "btc=1", "--cash-buffer-pct", "100"]):
            self.assertEqual(self.run_cli(argv)[0], 2, argv)

    def test_market_down_exit_3(self):
        with mock.patch.object(budget, "fetch_prices", side_effect=budget.MarketDown("down")):
            self.assertEqual(self.run_cli(["size", "btc_idr", "--idr", "300000"])[0], 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
