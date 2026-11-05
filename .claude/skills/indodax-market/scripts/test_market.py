#!/usr/bin/env python3
"""Offline tests for market.py (standard library only):  python3 test_market.py"""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market  # noqa: E402

TICKER = {"ticker": {"buy": "1460418000", "high": "1468996000", "last": "1460418000", "low": "1383000000",
                     "sell": "1460419000", "server_time": 1793850372, "vol_btc": "18.74446555",
                     "vol_idr": "27005507402"}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PairTests(unittest.TestCase):
    def test_formats_normalise_to_indodax_id(self):
        for p in ("btc_idr", "BTC/IDR", "btcidr", "BTC-IDR", " btc_idr "):
            self.assertEqual(market.pair_id(p), "btcidr")
        self.assertEqual(market.ticker_name("btcidr"), "btc_idr")
        self.assertEqual(market.ticker_name("ethusdt"), "eth_usdt")

    def test_rejects_unsafe_input(self):
        for p in ("", "btc;rm -rf", "../x", "a"):
            with self.assertRaises(market.InputError):
                market.pair_id(p)


class ParseTests(unittest.TestCase):
    def test_ticker_fields_spread_and_age(self):
        d = market.parse_ticker("btcidr", TICKER, now=1793850382)
        self.assertEqual((d["pair"], d["last"], d["bid"], d["ask"]),
                         ("btc_idr", "1460418000", "1460418000", "1460419000"))
        self.assertEqual(d["vol_base"], "18.74446555")
        self.assertAlmostEqual(d["spread_pct"], 0.0001, places=4)
        self.assertEqual(d["age_s"], 10)

    def test_unknown_pair_payload(self):
        with self.assertRaises(market.InputError):
            market.parse_ticker("zzzidr", {"error": "invalid_pair"})

    def test_orderbook_depth_and_totals(self):
        book = {"buy": [["100", "2"], ["99", "1"]], "sell": [["101", "1"], ["102", "3"]]}
        d = market.parse_orderbook("btcidr", book, depth=2)
        self.assertEqual((d["best_bid"], d["best_ask"]), ("100", "101"))
        self.assertEqual((d["bid_depth_idr"], d["ask_depth_idr"]), ("299", "407"))

    def test_trades_newest_first_and_limited(self):
        rows = [{"date": "1", "price": "10", "amount": "1", "tid": "a", "type": "buy"},
                {"date": "3", "price": "12", "amount": "1", "tid": "c", "type": "sell"},
                {"date": "2", "price": "11", "amount": "1", "tid": "b", "type": "buy"}]
        d = market.parse_trades("btcidr", rows, limit=2)
        self.assertEqual([t["tid"] for t in d["trades"]], ["c", "b"])


class FetchTests(unittest.TestCase):
    def test_cache_busting_and_no_credentials(self):
        seen = {}

        def fake(req, timeout):
            seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
            return _Resp(json.dumps(TICKER).encode())

        with mock.patch("urllib.request.urlopen", fake):
            market.ticker("btc_idr")
        self.assertIn("/api/ticker/btcidr?_=", seen["url"])
        self.assertNotIn("Key", seen["headers"])
        self.assertNotIn("Sign", seen["headers"])

    def test_retries_then_fails_with_market_error(self):
        calls = []

        def boom(req, timeout):
            calls.append(1)
            raise urllib.error.URLError("down")

        with mock.patch("urllib.request.urlopen", boom):
            with self.assertRaises(market.MarketError):
                market.fetch_json("/api/ticker/btcidr", sleep=lambda s: None)
        self.assertEqual(len(calls), market.RETRIES + 1)

    def test_404_is_input_error_not_retried(self):
        calls = []

        def nf(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

        with mock.patch("urllib.request.urlopen", nf):
            with self.assertRaises(market.InputError):
                market.fetch_json("/api/ticker/zzzidr", sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_cli_exit_codes(self):
        with mock.patch("urllib.request.urlopen", lambda r, timeout: _Resp(json.dumps(TICKER).encode())), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(market.main(["ticker", "btc_idr"]), 0)
        self.assertEqual(json.loads(out.getvalue())["pair"], "btc_idr")
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(market.main(["ticker", "bad;pair"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
