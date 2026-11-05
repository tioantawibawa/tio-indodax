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
        for k in ("INDODAX_LIVE", "INDODAX_LIVE_AUTO", "INDODAX_HALT", "INDODAX_ORDERS_FILE"):
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


class LiveCycleTests(Base):
    def setUp(self):
        super().setUp()
        self.bal = Path(self.tmp.name) / "fenced.json"
        self.sent = []

        def fake_order(a):
            r = json.loads(Path(a.risk).read_text())
            o = r["order"]
            self.sent.append((o["side"], o["pair"], a.confirm))
            return {"status": "filled", "mode": "live", "price": o["price"], "qty": o["amount"],
                    "filled_qty": o["amount"], "idr": o["idr"], "fee_idr": "1"}
        mock.patch.object(cycle.broker, "cmd_order", fake_order).start()

    def arm(self):
        os.environ["INDODAX_LIVE"] = "true"
        os.environ["INDODAX_LIVE_AUTO"] = cycle.LIVE_AUTO_PHRASE

    def write_bal(self, idr="100000", hold=None, book_idr=None, age=0):
        self.bal.write_text(json.dumps({"computed_at": cycle.time.time() - age, "idr": idr, "holdings": hold or {},
                                        "book": {"capital_idr": "100000", "idr": book_idr or idr, "holdings": hold or {}}}))

    def live(self, **kw):
        return cycle.run(["btc_idr", "eth_idr"], "240", D(5), False, signal_fn=lambda p, tf: self.signals[p],
                         live=True, balance_file=str(self.bal), **kw)

    def test_needs_both_switches(self):
        self.write_bal()
        with self.assertRaises(cycle.Refused):
            self.live()                                        # nothing armed
        os.environ["INDODAX_LIVE"] = "true"
        with self.assertRaises(cycle.Refused):
            self.live()                                        # INDODAX_LIVE alone is not enough
        with self.assertRaises(cycle.Refused):
            self.run_cycle()                                   # and the paper path refuses when armed
        self.assertEqual(self.sent, [])

    def test_live_buy_uses_fenced_balance_and_auto_confirm(self):
        self.arm()
        self.write_bal(idr="100000")
        d = self.live()
        self.assertEqual(self.sent, [("buy", "btc_idr", True)])
        btc = [r for r in d["rows"] if r["pair"] == "btc_idr"][0]
        self.assertLessEqual(D(btc["result"]["idr"]), D(25000))       # cap 25 % of the fenced 100k, not the account
        self.assertTrue(d["live"] and "LIVE" in d["text"])
        self.assertEqual(cycle.load_positions()["btc"]["qty"], btc["result"]["filled_qty"])

    def test_small_fence_means_no_order(self):
        self.arm()
        self.write_bal(idr="9000")                             # agent's share leaves almost nothing
        d = self.live()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.actions(d)["btc_idr"], ("buy", "no-budget"))

    def test_stale_balance_file_refused(self):
        self.arm()
        self.write_bal(age=600)
        with self.assertRaises(cycle.Refused):
            self.live()

    def test_drawdown_creates_halt_buys_stop_sells_continue(self):
        self.arm()
        self.write_bal(idr="80000", book_idr="80000")          # book equity 80k < 85k
        d = self.live()
        self.assertTrue((Path(self.tmp.name) / "HALT").exists())
        self.assertIn("HALT", d["text"])
        self.assertEqual(self.actions(d)["btc_idr"], ("buy", "blocked"))   # risk kill switch
        self.assertEqual(self.sent, [])

    def test_stops_mode_only_exits(self):
        self.arm()
        self.write_bal(idr="80000", hold={"btc": "0.01"}, book_idr="90000")
        cycle.save_state({"btc": {"entry": "1100000", "stop": "1045000", "qty": "0.01"}}, {}, {"ran_at": 1})
        d = cycle.run([], "-", D(5), False, live=True, balance_file=str(self.bal), exits_only=True)
        self.assertEqual(self.sent, [("sell", "btc_idr", True)])          # price 1,000,000 <= stop
        self.assertEqual(d["rows"][-1]["action"], "sell")
        self.assertEqual(cycle.load_state()["last_run"], {"ran_at": 1})   # hourly heartbeat untouched
        self.assertIn("btc", cycle.load_state()["cooldown_until"])

    def test_apply_fill_matches_broker_book(self):
        idr, hold = cycle.apply_fill(D(100000), {}, "btc", "buy", D(1000000), D("0.02"))
        f = cycle.broker.FEE
        self.assertEqual(idr, D(100000) - D(20000) * (1 + f))
        self.assertEqual(hold["btc"], D("0.02") * (1 - f))


class LiveEndToEndTests(Base):
    """cycle -> risk -> REAL broker (signing, settle, ledger, book) -> fake Indodax /tapi."""

    def setUp(self):
        super().setUp()
        self.orders = {}
        self.calls = []
        mock.patch("urllib.request.urlopen", self.exchange).start()      # overrides the offline guard
        mock.patch.object(cycle.broker, "server_offset_ms", return_value=0).start()
        os.environ.update({"INDODAX_LIVE": "true", "INDODAX_LIVE_AUTO": cycle.LIVE_AUTO_PHRASE,
                           "INDODAX_API_KEY": "k-test", "INDODAX_API_SECRET": "s-test"})
        cycle.broker.cmd_live_init(type("A", (), {"capital_idr": "100000"})())
        self.bal = Path(self.tmp.name) / "fenced.json"

    def exchange(self, req, timeout):
        import urllib.parse
        q = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.calls.append(q["method"])
        coin = q.get("pair", "btc_idr").split("_")[0]
        if q["method"] == "trade":
            oid = str(100 + len(self.orders))
            self.orders[oid] = {"order_id": oid, "price": q["price"], f"order_{coin}": q[coin],
                                f"remain_{coin}": "0", "status": "filled"}
            ret = {"order_id": int(oid)}
        elif q["method"] == "getOrder":
            ret = {"order": self.orders[q["order_id"]]}
        else:
            raise AssertionError(f"unexpected private call {q['method']}")
        return io.BytesIO(json.dumps({"success": 1, "return": ret}).encode())

    def fence_from_book(self):
        b = cycle.broker.live_book()
        self.bal.write_text(json.dumps({"computed_at": cycle.time.time(), "idr": b["idr"],
                                        "holdings": b["holdings"], "book": b}))

    def test_full_live_round_trip(self):
        self.fence_from_book()
        d = cycle.run(["btc_idr", "eth_idr"], "240", D(5), False, signal_fn=lambda p, tf: self.signals[p],
                      live=True, balance_file=str(self.bal))
        self.assertEqual(self.calls, ["trade", "getOrder"])                # one order, settled, nothing left open
        ledger = json.loads((Path(self.tmp.name) / "orders.json").read_text())
        self.assertEqual((ledger[-1]["mode"], ledger[-1]["status"]), ("live", "filled"))
        book = cycle.broker.live_book()
        self.assertLess(D(book["idr"]), D(100000))
        self.assertIn("btc", book["holdings"])
        self.assertEqual(d["orders"], 1)
        # an hour later: price falls through the stop -> the 5-minute stop check sells the skills' own BTC only
        self.prices["btc"] = D(900000)
        self.fence_from_book()
        cycle.run([], "-", D(5), False, live=True, balance_file=str(self.bal), exits_only=True)
        self.assertEqual(self.calls[-2:], ["trade", "getOrder"])
        sell = json.loads((Path(self.tmp.name) / "orders.json").read_text())[-1]
        self.assertEqual(sell["side"], "sell")
        self.assertLessEqual(D(sell["qty"]), D(book["holdings"]["btc"]))     # never more than the book owns
        self.assertNotIn("btc", cycle.broker.live_book()["holdings"])
        self.assertIn("btc", cycle.load_state()["cooldown_until"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
