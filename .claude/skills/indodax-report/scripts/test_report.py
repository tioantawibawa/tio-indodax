#!/usr/bin/env python3
"""Offline tests for report.py (no network, fake Telegram token):  python3 test_report.py"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402

TOKEN, CHAT = "123456:FAKE-token-not-real", "987"
TODAY = "2026-11-05"
NOW = 1793851200                                       # 2026-11-05 WIB


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        mock.patch.dict(os.environ, {"INDODAX_STATE_DIR": self.tmp.name}).start()
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "INDODAX_LIVE", "INDODAX_HALT", "INDODAX_ORDERS_FILE"):
            os.environ.pop(k, None)
        mock.patch.object(report.budget, "fetch_prices",
                          lambda coins: report.budget.manual_prices({c: D("1000000") for c in coins})).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def write(self, name, data):
        (self.dir / name).write_text(json.dumps(data))

    def cli(self, argv, stdin=""):
        with mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = report.main(argv)
        return rc, out.getvalue(), err.getvalue()


class DailyTests(Base):
    def seed(self):
        self.write("paper_balance.json", {"idr": "250000", "holdings": {"btc": "0.05"}})
        self.write("risk_state.json", {"date": TODAY, "start_equity_idr": "310000"})
        self.write("orders.json", [
            {"ts": NOW - 3600, "date_wib": TODAY, "mode": "paper", "pair": "btc_idr", "side": "buy", "price": "1000000",
             "qty": "0.05", "idr": "50000", "status": "filled", "fee_idr": "172", "client_order_id": "sk-1"},
            {"ts": NOW - 1800, "date_wib": TODAY, "mode": "live", "pair": "eth_idr", "side": "buy", "price": "50000000",
             "qty": "0.0004", "idr": "20000", "status": "unknown", "client_order_id": "sk-2", "error": "jaringan"},
            {"ts": NOW - 90000, "date_wib": "2026-11-04", "mode": "paper", "pair": "btc_idr", "side": "sell",
             "price": "1", "qty": "1", "idr": "1", "status": "filled"}])
        self.write("alerts.json", [{"id": "a1", "kind": "price", "pair": "btc_idr", "op": "above", "level": 2e9}])

    def test_summary_numbers(self):
        self.seed()
        d = report.collect(TODAY, "paper", NOW)
        self.assertEqual(d["balance"]["equity_idr"], 300000)
        self.assertEqual(d["balance"]["coins"][0]["pct"], 16.7)
        self.assertEqual((d["day"]["pnl_idr"], d["day"]["pnl_pct"]), (-10000, -3.23))
        o = d["orders"]
        self.assertEqual((o["count"], o["buy_idr"], o["fees_idr"]), (2, 50000, 172))   # unknown not counted as done
        self.assertEqual([r["client_order_id"] for r in o["attention"]], ["sk-2"])
        text = report.render(d)
        for s in ("Equity Rp 300.000", "⛔ batas harian tercapai", "⚠️ cek eth_idr buy sk-2: unknown", "BTC/IDR ≥ Rp 2.000.000.000"):
            self.assertIn(s, text)

    def test_missing_state_is_graceful(self):
        d = report.collect(TODAY, "paper", NOW)
        self.assertIsNone(d["balance"])
        self.assertIn("paper-init", d["warnings"][0])
        self.assertIn("Order: tidak ada hari ini", report.render(d))

    def test_halt_shown(self):
        self.seed()
        (self.dir / "HALT").touch()
        self.assertIn("Kill switch AKTIF", report.render(report.collect(TODAY, "paper", NOW)))

    def test_bad_date(self):
        self.assertEqual(self.cli(["daily", "--date", "05-11-2026"])[0], 2)


class SendTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = TOKEN, CHAT

    def test_send_alerts_json_and_no_token_leak(self):
        seen = []

        def fake(req, timeout):
            seen.append((req.full_url, dict(urllib.parse.parse_qsl(req.data.decode()))))
            return _Resp(b'{"ok": true}')

        with mock.patch("urllib.request.urlopen", fake):
            rc, out, err = self.cli(["send", "--stdin"], json.dumps({"text": "Indodax alert\nBTC naik"}))
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0][1], {"chat_id": CHAT, "text": "Indodax alert\nBTC naik", "disable_web_page_preview": "true"})
        self.assertTrue(seen[0][0].endswith("/sendMessage"))
        self.assertNotIn(TOKEN, out + err)

    def test_empty_alerts_sends_nothing(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not send")):
            self.assertEqual(self.cli(["send", "--stdin"], json.dumps({"text": ""}))[0], 0)

    def test_dry_run_and_not_configured(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not send")):
            rc, out, _ = self.cli(["send", "--text", "halo", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        del os.environ["TELEGRAM_BOT_TOKEN"]
        self.assertEqual(self.cli(["send", "--text", "halo"])[0], 1)

    def test_long_text_split(self):
        parts = report.chunks("\n".join("x" * 100 for _ in range(100)))
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) <= report.TG_LIMIT for p in parts))
        self.assertEqual(sum(p.count("x") for p in parts), 10000)

    def test_429_retried_then_error_redacted(self):
        calls = []

        def busy(req, timeout):
            calls.append(1)
            body = io.BytesIO(json.dumps({"ok": False, "description": f"Too Many Requests bot{TOKEN}",
                                          "parameters": {"retry_after": 1}}).encode())
            raise urllib.error.HTTPError(req.full_url, 429, "busy", {}, body)

        with mock.patch("urllib.request.urlopen", busy), mock.patch("time.sleep"):
            with self.assertRaises(report.SendError) as cm:
                report.send_telegram("hi", sleep=lambda s: None)
        self.assertEqual(len(calls), 3)
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_400_not_retried(self):
        calls = []

        def bad(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"description":"chat not found"}'))

        with mock.patch("urllib.request.urlopen", bad):
            rc, _, err = self.cli(["send", "--text", "hi"])
        self.assertEqual((rc, len(calls)), (3, 1))
        self.assertIn("chat not found", err)


if __name__ == "__main__":
    unittest.main(verbosity=1)
