#!/usr/bin/env python3
"""Offline tests for alerts.py (standard library only, no network):  python3 test_alerts.py"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alerts  # noqa: E402

NOW = 1_800_000_000


def series(closes, tf_s, end=NOW):
    """Closed candles ending at `end`, oldest first."""
    n = len(closes)
    return [{"time": end - (n - i) * tf_s, "close": str(c)} for i, c in enumerate(closes)]


class FakeMarket:
    def __init__(self, prices, candles=None):
        self.prices, self.cs, self.calls = prices, candles or {}, []

    def ticker(self, pair):
        self.calls.append(("t", pair))
        return {"last": str(self.prices[pair]), "age_s": 1}

    def candles(self, pair, tf, limit, now=None):
        self.calls.append(("c", pair, tf))
        return {"candles": self.cs[(pair, tf)][-limit:]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INDODAX_STATE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def cli(self, *argv):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            rc = alerts.main(list(argv))
        return rc, out.getvalue()

    def run_check(self, fake, now=NOW):
        with mock.patch.object(alerts.market, "ticker", fake.ticker), \
                mock.patch.object(alerts.market, "candles", fake.candles):
            return alerts.check(now=now)


class RuleTests(Base):
    def test_add_list_remove_and_dedup(self):
        self.assertEqual(self.cli("add", "price", "BTC/IDR", "--above", "1500000000")[0], 0)
        rc, out = self.cli("add", "price", "btc_idr", "--above", "1500000000")
        self.assertEqual(json.loads(out)["added"], False)
        self.cli("add", "move", "eth_idr", "--pct", "5")
        rules = json.loads(self.cli("list")[1])["rules"]
        self.assertEqual([r["id"] for r in rules], ["a1", "a2"])
        self.assertEqual(self.cli("remove", "a1")[0], 0)
        self.assertEqual(self.cli("remove", "a1")[0], 2)
        self.assertEqual(self.cli("add", "move", "sol_idr", "--pct", "3")[1] and
                         json.loads(self.cli("list")[1])["rules"][-1]["id"], "a3")      # ids never reused

    def test_validation(self):
        for argv in (["add", "price", "btc_idr"], ["add", "price", "btc_idr", "--above", "1", "--below", "2"],
                     ["add", "rsi", "btc_idr", "--above", "150"], ["add", "move", "btc_idr", "--pct", "0"],
                     ["add", "cross", "btc_idr", "--fast", "26", "--slow", "12"],
                     ["add", "price", "btc;rm", "--above", "1"], ["add", "price", "eth_usdt", "--above", "1"]):
            self.assertEqual(self.cli(*argv)[0], 2, argv)


class CheckTests(Base):
    def test_price_alert_is_edge_triggered(self):
        self.cli("add", "price", "btc_idr", "--above", "100")
        seq = [(99, 0), (101, 1), (105, 0), (98, 0), (102, 1)]                 # fire, hold, re-arm, fire
        for px, want in seq:
            d = self.run_check(FakeMarket({"btc_idr": px}))
            self.assertEqual(len(d["triggered"]), want, px)
        self.assertIn("Rp 102", d["text"])

    def test_move_alert_and_cooldown(self):
        self.cli("add", "move", "btc_idr", "--pct", "5", "--window", "60", "--cooldown", "30")
        cs = {("btc_idr", "1"): series([100.0] * 120, 60)}
        fake = FakeMarket({"btc_idr": 106}, cs)
        d = self.run_check(fake)
        self.assertEqual(len(d["triggered"]), 1)
        self.assertIn("+6.00%", d["text"])
        self.assertEqual(self.run_check(fake, NOW + 600)["triggered"], [])        # cooldown
        cs[("btc_idr", "1")] = series([100.0] * 120, 60, end=NOW + 1800)
        self.assertEqual(len(self.run_check(fake, NOW + 1800)["triggered"]), 1)   # after cooldown
        self.assertEqual(self.run_check(FakeMarket({"btc_idr": 103}, cs), NOW + 4000)["triggered"], [])
        down = self.run_check(FakeMarket({"btc_idr": 94}, cs), NOW + 4000)["triggered"]
        self.assertIn("turun -6.00%", down[0]["message"])

    def test_move_timeframe_choice(self):
        self.assertEqual([alerts.move_tf(w) for w in (60, 360, 361, 4320, 4321)], ["1", "1", "15", "15", "60"])

    def test_rsi_zone_entry(self):
        self.cli("add", "rsi", "eth_idr", "--tf", "60", "--above", "70")
        up = series([100 + i for i in range(200)], 3600)
        d = self.run_check(FakeMarket({}, {("eth_idr", "60"): up}))
        self.assertEqual(len(d["triggered"]), 1)
        self.assertIn("overbought", d["text"])
        self.assertEqual(self.run_check(FakeMarket({}, {("eth_idr", "60"): up}))["triggered"], [])

    def test_cross_fires_once_per_candle(self):
        self.cli("add", "cross", "sol_idr", "--tf", "60", "--fast", "3", "--slow", "10")
        closes = [200 - i for i in range(60)]
        cs = {("sol_idr", "60"): series(closes, 3600)}
        self.assertEqual(self.run_check(FakeMarket({}, cs))["triggered"], [])
        fired = []
        for k in range(1, 15):                        # sharp rally → exactly one golden cross
            cs[("sol_idr", "60")] = series(closes + [141 + 6 * i for i in range(k)], 3600, end=NOW + k * 3600)
            fired += self.run_check(FakeMarket({}, cs), NOW + k * 3600)["triggered"]
            self.run_check(FakeMarket({}, cs), NOW + k * 3600 + 60)               # same candle again: no repeat
        self.assertEqual(len(fired), 1)
        self.assertIn("golden cross", fired[0]["message"])

    def test_data_fetched_once_per_pair(self):
        self.cli("add", "price", "btc_idr", "--above", "1")
        self.cli("add", "price", "btc_idr", "--below", "1000")
        fake = FakeMarket({"btc_idr": 50})
        self.run_check(fake)
        self.assertEqual(fake.calls, [("t", "btc_idr")])

    def test_partial_and_total_market_failure(self):
        self.cli("add", "price", "btc_idr", "--above", "1")
        self.cli("add", "price", "eth_idr", "--above", "1")

        class Half(FakeMarket):
            def ticker(self, pair):
                if pair == "eth_idr":
                    raise alerts.market.MarketError("down")
                return super().ticker(pair)

        d = self.run_check(Half({"btc_idr": 5}))
        self.assertEqual((len(d["triggered"]), d["errors"][0]["id"]), (1, "a2"))
        with mock.patch.object(alerts.market, "ticker", side_effect=alerts.market.MarketError("down")), \
                mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(alerts.main(["check"]), 3)

    def test_no_rules_no_text(self):
        d = self.run_check(FakeMarket({}))
        self.assertEqual((d["rules"], d["text"]), (0, ""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
