#!/usr/bin/env python3
"""Offline tests for analyst.py (standard library only):  python3 test_analyst.py"""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyst  # noqa: E402


def candles_json(closes, pair="btc_idr", tf="1D"):
    return {"pair": pair, "tf": tf, "count": len(closes),
            "candles": [{"time": i * 86400, "open": str(c), "high": str(c), "low": str(c), "close": str(c),
                         "volume": "1"} for i, c in enumerate(closes)]}


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(analyst.sma([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])

    def test_ema_seeded_with_sma(self):
        self.assertEqual(analyst.ema([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])
        self.assertEqual(analyst.ema([2, 4, 6, 10], 3), [None, None, 4, 7])
        self.assertEqual(analyst.ema([1, 2], 3), [None, None])

    def test_rsi_wilder_by_hand(self):
        # changes +1 -1 +1 -1, n=2: 50 → gain .75/loss .25 = 75 → .375/.625 = 37.5
        self.assertEqual(analyst.rsi([1, 2, 1, 2, 1], 2), [None, None, 50.0, 75.0, 37.5])

    def test_rsi_edges(self):
        self.assertEqual(analyst.rsi([1, 2, 3, 4], 3)[-1], 100.0)
        self.assertEqual(analyst.rsi([5, 5, 5, 5], 3)[-1], 50.0)
        self.assertEqual(analyst.rsi([4, 3, 2, 1], 3)[-1], 0.0)
        self.assertEqual(analyst.rsi([1, 2, 3], 3), [None, None, None])

    def test_bad_period(self):
        for f in (analyst.sma, analyst.ema, analyst.rsi):
            with self.assertRaises(ValueError):
                f([1, 2, 3], 0)


class AnalyseTests(unittest.TestCase):
    def test_uptrend_is_bullish(self):
        closes = [100 + i for i in range(60)]
        d = analyst.analyse(*analyst.series_from(candles_json(closes)))
        self.assertEqual(d["bias"], "bullish")
        self.assertEqual(d["signals"]["ema_cross"], "bullish")
        self.assertEqual(d["signals"]["rsi_zone"], "overbought")
        self.assertEqual(d["indicators"]["sma20"], sum(closes[-20:]) / 20)
        self.assertIn("bukan perintah order", d["note"])

    def test_downtrend_is_bearish(self):
        d = analyst.analyse(*analyst.series_from(candles_json([200 - i for i in range(60)])))
        self.assertEqual((d["bias"], d["signals"]["rsi_zone"]), ("bearish", "oversold"))

    def test_fresh_cross_detected(self):
        closes = [200 - i for i in range(50)] + [150 + 8 * i for i in range(1, 30)]
        flags = []
        for k in range(30, len(closes) + 1):
            d = analyst.analyse(*analyst.series_from(candles_json(closes[:k])), sma_p=(5,), ema_p=(3, 10))
            flags.append(d["signals"]["fresh_cross"])
        self.assertEqual(flags.count(True), 1)

    def test_not_enough_data(self):
        with self.assertRaises(analyst.InputError):
            analyst.analyse(*analyst.series_from(candles_json(list(range(1, 40)))))

    def test_trades_sorted_oldest_first(self):
        data = {"pair": "btc_idr", "trades": [{"time": 3, "price": "30", "tid": "c"},
                                              {"time": 1, "price": "10", "tid": "a"},
                                              {"time": 2, "price": "20", "tid": "b"}]}
        times, closes, pair, src = analyst.series_from(data)
        self.assertEqual((times, closes, src), ([1, 2, 3], [10.0, 20.0, 30.0], "trades"))

    def test_unknown_input(self):
        with self.assertRaises(analyst.InputError):
            analyst.series_from({"ticker": {}})


class CliTests(unittest.TestCase):
    def run_cli(self, argv, stdin=""):
        with mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = analyst.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_pipe_from_market(self):
        rc, out, _ = self.run_cli(["--input", "-"], json.dumps(candles_json([100 + i for i in range(60)])))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["bias"], "bullish")

    def test_errors_exit_2(self):
        self.assertEqual(self.run_cli(["--input", "-"], "{}")[0], 2)
        self.assertEqual(self.run_cli(["--input", "-"], "not json")[0], 2)
        self.assertEqual(self.run_cli(["--input", "-", "--sma", "a,b"], "{}")[0], 2)
        self.assertEqual(self.run_cli([])[0], 2)

    def test_market_down_exit_3(self):
        sys.path.insert(0, str(analyst.MARKET_DIR))
        import market
        with mock.patch.object(market, "candles", side_effect=market.MarketError("down")):
            self.assertEqual(self.run_cli(["btc_idr"])[0], 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
