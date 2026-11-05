#!/usr/bin/env python3
"""Offline tests for cycle.py (no network):  python3 test_cycle.py"""

import fcntl
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
import cycle  # noqa: E402

RULES = {"tick": D("1"), "qty_step": D("0.00000001"), "min_idr": D(10000), "min_qty": D(0), "tradable": True}


def sig(bias="bullish", rsi="neutral"):
    return {"bias": bias, "signals": {"rsi_zone": rsi}, "reasons": [f"uji {bias}"]}


class Base(unittest.TestCase):
    def setUp(self):
        mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used in offline test")).start()
        self.tmp = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"INDODAX_STATE_DIR": self.tmp.name}).start()
        for k in ("INDODAX_LIVE", "INDODAX_HALT", "INDODAX_ORDERS_FILE"):
            os.environ.pop(k, None)
        self.prices = {"btc": D(1000000), "eth": D(50000)}
        mock.patch.object(cycle.budget, "fetch_prices",
                          lambda coins: cycle.budget.manual_prices({c: self.prices[c] for c in coins})).start()
        mock.patch.object(cycle.broker, "pair_rules", return_value=RULES).start()
        self.signals = {"btc_idr": sig(), "eth_idr": sig(rsi="overbought")}
        cycle.broker.cmd_paper_init(type("A", (), {"idr": "300000", "hold": None, "reset": False})())

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def run_cycle(self, dry=False):
        return cycle.run(["btc_idr", "eth_idr"], "240", D(5), dry, signal_fn=lambda p, tf: self.signals[p])

    def actions(self, d):
        return {r["pair"]: (r["action"], (r.get("result") or {}).get("status")) for r in d["rows"]}


class CycleTests(Base):
    def test_buy_bullish_skip_overbought_then_hold(self):
        d = self.run_cycle()
        self.assertEqual(self.actions(d), {"btc_idr": ("buy", "filled"), "eth_idr": ("wait", None)})
        pos = cycle.load_positions()["btc"]
        self.assertEqual((pos["entry"], pos["stop"]), ("1000000", "950000"))
        self.assertEqual(d["orders"], 1)
        d2 = self.run_cycle()                                         # no pyramiding
        self.assertEqual(self.actions(d2)["btc_idr"][0], "hold")
        self.assertEqual(d2["orders"], 0)

    def test_stop_hit_sells(self):
        self.run_cycle()
        self.prices["btc"] = D(940000)
        d = self.run_cycle()
        self.assertEqual(d["rows"][0]["pair"], "btc_idr")
        self.assertEqual((d["rows"][0]["action"], d["rows"][0]["result"]["status"]), ("sell", "filled"))
        self.assertIn("stop", d["rows"][0]["reason"])
        self.assertEqual(cycle.load_positions(), {})
        self.assertNotIn("btc", json.loads((Path(self.tmp.name) / "paper_balance.json").read_text())["holdings"])
        self.assertEqual([r["action"] for r in d["rows"] if r["pair"] == "btc_idr"], ["sell", "wait"])  # no instant rebuy
        self.assertIn("jeda", d["rows"][1]["reason"])
        with mock.patch.object(cycle.time, "time", return_value=cycle.time.time() + 25 * 3600):
            self.assertEqual(self.actions(self.run_cycle())["btc_idr"], ("buy", "filled"))      # after cooldown

    def test_bearish_sells_and_does_not_rebuy_same_run(self):
        self.run_cycle()
        self.signals["btc_idr"] = sig("bearish")
        d = self.run_cycle()
        self.assertEqual(self.actions(d)["btc_idr"], ("wait", None))   # last row for btc: no rebuy (bearish)
        self.assertEqual([r["action"] for r in d["rows"] if r["pair"] == "btc_idr"], ["sell", "wait"])

    def test_dry_run_changes_nothing(self):
        d = self.run_cycle(dry=True)
        self.assertEqual(self.actions(d)["btc_idr"], ("buy", "dry-run"))
        self.assertFalse((Path(self.tmp.name) / "orders.json").exists())
        self.assertEqual(cycle.load_positions(), {})

    def test_kill_switch_blocks_buys(self):
        (Path(self.tmp.name) / "HALT").touch()
        d = self.run_cycle()
        self.assertEqual(self.actions(d)["btc_idr"], ("buy", "blocked"))
        self.assertEqual(d["rows"][0]["result"]["failed"], ["kill_switch"])
        self.assertTrue(d["problems"])

    def test_signal_failure_skips_pair(self):
        def sf(p, tf):
            if p == "eth_idr":
                raise cycle.market.MarketError("down")
            return sig()
        d = cycle.run(["btc_idr", "eth_idr"], "240", D(5), False, signal_fn=sf)
        self.assertEqual(self.actions(d)["eth_idr"], ("skip", None))
        self.assertEqual(self.actions(d)["btc_idr"], ("buy", "filled"))
        def down(p, tf):
            raise cycle.market.MarketError("down")
        with self.assertRaises(cycle.budget.MarketDown):
            cycle.run(["btc_idr", "eth_idr"], "240", D(5), False, signal_fn=down)

    def test_live_armed_refused(self):
        os.environ["INDODAX_LIVE"] = "true"
        with self.assertRaises(cycle.Refused):
            self.run_cycle()

    def cli(self, *argv):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = cycle.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_lock_prevents_overlap(self):
        with open(Path(self.tmp.name) / "cycle.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            rc, _, err = self.cli("run", "--pairs", "btc_idr")
        self.assertEqual(rc, 1)
        self.assertIn("berjalan", err)

    def test_cli_validation(self):
        self.assertEqual(self.cli("run", "--pairs", "eth_usdt")[0], 2)
        self.assertEqual(self.cli("run", "--stop-pct", "50")[0], 2)

    def test_notify_only_when_something_happened(self):
        sent = []
        with mock.patch.object(cycle, "get_signal", lambda p, tf: self.signals[p]), \
                mock.patch.object(cycle.report, "send_telegram", lambda text: sent.append(text) or 1):
            self.assertEqual(self.cli("run", "--pairs", "btc_idr,eth_idr", "--notify")[0], 0)
            self.assertEqual(self.cli("run", "--pairs", "btc_idr,eth_idr", "--notify")[0], 0)   # hold only
        self.assertEqual(len(sent), 1)
        self.assertIn("BTC_IDR BUY", sent[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
