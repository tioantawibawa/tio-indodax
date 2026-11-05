#!/usr/bin/env python3
"""Offline tests for risk.py (standard library only):  python3 test_risk.py"""

import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import risk  # noqa: E402

PX = risk.budget.manual_prices({"btc": D("1000000"), "eth": D("50000")})
LIM = dict(risk.LIMITS)


def buy(idr, stop=D(5), coin="btc", price=D("1000000")):
    return {"pair": f"{coin}_idr", "coin": coin, "side": "buy", "idr": D(idr), "amount": D(idr) / price,
            "price": price, "stop_pct": stop}


def failed(order, idr=D(300000), hold=None, start=D(300000), halt=None, px=PX):
    return risk.evaluate(order, idr, hold or {}, px, LIM, start, halt)["failed"]


def setUpModule():
    p = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used in offline test"))
    p.start()
    unittest.addModuleCleanup(p.stop)


class EvaluateTests(unittest.TestCase):
    def test_clean_buy_allowed(self):
        d = risk.evaluate(buy(50000), D(300000), {}, PX, LIM, D(300000))
        self.assertEqual(d["decision"], "ALLOW")
        self.assertEqual(len(d["order_fingerprint"]), 16)

    def test_stop_required(self):
        self.assertEqual(failed(buy(20000, stop=None)), ["stop_loss_set", "max_loss_per_trade"])

    def test_max_loss_per_trade_boundary(self):
        # loss = idr × (5% + 2×0.3422%) ; 1% of 300k = 3000 → max idr 52,776.02
        self.assertEqual(failed(buy(52776)), [])
        self.assertEqual(failed(buy(52777)), ["max_loss_per_trade"])

    def test_coin_and_total_exposure(self):
        hold = {"btc": D("0.06")}                                    # 60k of 300k equity
        self.assertEqual(failed(buy(16000), idr=D(240000), hold=hold), ["max_coin_exposure"])
        hold = {"btc": D("0.07"), "eth": D("3.4")}                   # 70k + 170k of 300k
        self.assertIn("max_total_exposure", failed(buy(10000, coin="eth", price=D(50000)), idr=D(60000), hold=hold))

    def test_daily_loss_limit_blocks_buy_not_sell(self):
        self.assertEqual(failed(buy(20000), start=D(310000)), ["daily_loss_limit"])      # −3.2 %
        self.assertEqual(failed(buy(20000), start=D(309000)), [])                        # −2.9 %
        sell = {"pair": "btc_idr", "coin": "btc", "side": "sell", "idr": D(20000), "amount": D("0.02"),
                "price": D(1000000), "stop_pct": None}
        self.assertEqual(failed(sell, hold={"btc": D("0.05")}, start=D(400000)), [])

    def test_kill_switch(self):
        self.assertEqual(failed(buy(20000), halt="manual"), ["kill_switch"])

    def test_price_cash_minimum(self):
        self.assertEqual(failed(buy(20000, price=D(1030000))), ["price_vs_market"])
        self.assertIn("enough_idr", failed(buy(20000), idr=D(15000), hold={"eth": D(1)}))
        self.assertEqual(failed(buy(5000)), ["min_order"])

    def test_sell_more_than_holding(self):
        sell = {"pair": "btc_idr", "coin": "btc", "side": "sell", "idr": D(20000), "amount": D("0.02"),
                "price": D(1000000), "stop_pct": None}
        self.assertEqual(failed(sell, hold={"btc": D("0.01")}), ["enough_coin"])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INDODAX_STATE_DIR": self.tmp.name, "INDODAX_HALT": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_day_start_recorded_once_per_wib_day(self):
        t = 1793800000                                   # 2026-11-04 WIB
        self.assertEqual(risk.day_start(D(300000), t)[1], D(300000))
        self.assertEqual(risk.day_start(D(280000), t + 3600)[1], D(300000))       # same day keeps start
        self.assertEqual(risk.day_start(D(280000), t + 86400)[1], D(280000))      # next day resets

    def test_halt_file_and_env(self):
        self.assertIsNone(risk.halted())
        (Path(self.tmp.name) / "HALT").touch()
        self.assertIn("HALT", risk.halted())
        with mock.patch.dict(os.environ, {"INDODAX_HALT": "1"}):
            self.assertIn("INDODAX_HALT", risk.halted())

    def run_cli(self, argv, stdin=""):
        with mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            return risk.main(argv), out.getvalue()

    def test_cli_exit_codes(self):
        base = ["--idr", "300000", "--price", "btc=1000000"]
        self.assertEqual(self.run_cli(["check", "btc_idr", "--idr-order", "20000", "--stop-pct", "5", *base])[0], 0)
        self.assertEqual(self.run_cli(["check", "btc_idr", "--idr-order", "20000", *base])[0], 1)
        self.assertEqual(self.run_cli(["check", "btc_idr", "--idr-order", "20000", "--stop-pct", "5"])[0], 2)  # no balance
        self.assertEqual(self.run_cli(["check", "btc_idr", "--idr-order", "20000", "--stop-price", "2000000", *base])[0], 2)

    def test_pipe_from_budget(self):
        order = {"pair": "btc_idr", "side": "buy", "order_idr": "40000", "est_amount": "0.0399", "price": "1000000",
                 "ok": True}
        rc, out = self.run_cli(["check", "--order", "-", "--stop-pct", "5", "--idr", "300000", "--price", "btc=1000000"],
                               json.dumps(order))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["order"]["idr"], "40000")
        bad = {**order, "ok": False, "reason": "x"}
        self.assertEqual(self.run_cli(["check", "--order", "-", "--idr", "1"], json.dumps(bad))[0], 2)

    def test_stop_price_converted(self):
        rc, out = self.run_cli(["check", "btc_idr", "--idr-order", "20000", "--stop-price", "950000",
                                "--idr", "300000", "--price", "btc=1000000"])
        self.assertEqual((rc, json.loads(out)["order"]["stop_pct"]), (0, "5.0000"))

    def test_market_down_exit_3(self):
        with mock.patch.object(risk.budget, "fetch_prices", side_effect=risk.MarketDown("down")):
            self.assertEqual(self.run_cli(["check", "btc_idr", "--idr-order", "20000", "--idr", "300000"])[0], 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
