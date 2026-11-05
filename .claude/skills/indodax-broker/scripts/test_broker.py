#!/usr/bin/env python3
"""Offline tests for broker.py (standard library only; no network, no real keys):  python3 test_broker.py"""

import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import broker  # noqa: E402

RULES = {"tick": D("1000"), "qty_step": D("0.00000001"), "min_idr": D(10000), "min_qty": D("0.000006"),
         "tradable": True}
FAKE_KEY, FAKE_SECRET = "TESTKEY-not-real", "testsecret-not-real"


def approval(side="buy", idr="50000", amount="0.00005", price="1000000000", age=0, decision="ALLOW"):
    o = {"pair": "btc_idr", "side": side, "idr": idr, "amount": amount, "price": price, "stop_pct": "5"}
    return {"decision": decision, "failed": [] if decision == "ALLOW" else ["x"], "order": o,
            "order_fingerprint": broker.risk.order_fingerprint(o), "checked_at": int(time.time()) - age}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"INDODAX_STATE_DIR": self.tmp.name}, clear=False)
        self.env.start()
        for k in ("INDODAX_LIVE", "INDODAX_API_KEY", "INDODAX_API_SECRET", "INDODAX_HALT", "INDODAX_ORDERS_FILE"):
            os.environ.pop(k, None)
        self.rules = mock.patch.object(broker, "pair_rules", return_value=RULES)
        self.rules.start()
        self.offset = mock.patch.object(broker, "server_offset_ms", return_value=0)
        self.offset.start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def cli(self, argv, stdin=""):
        with mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = broker.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def order(self, appr, *extra):
        return self.cli(["order", "--risk", "-", *extra], json.dumps(appr))


class GateTests(Base):
    def test_block_stale_tampered_replayed(self):
        with self.assertRaises(broker.Refused):
            broker.verify_approval(approval(decision="BLOCK"), [], time.time(), 60)
        with self.assertRaises(broker.Refused):
            broker.verify_approval(approval(age=120), [], time.time(), 60)
        a = approval()
        a["order"]["idr"] = "500000"                                          # edited after risk check
        with self.assertRaisesRegex(broker.Refused, "fingerprint"):
            broker.verify_approval(a, [], time.time(), 60)
        a = approval()
        with self.assertRaisesRegex(broker.Refused, "sudah pernah"):
            broker.verify_approval(a, [{"risk_fingerprint": a["order_fingerprint"], "status": "filled"}], time.time(), 60)
        broker.verify_approval(a, [{"risk_fingerprint": a["order_fingerprint"], "status": "rejected"}], time.time(), 60)

    def test_rounding_never_worse(self):
        self.assertEqual(broker.round_order("buy", D("1000000999"), D("0.000050019"), RULES), (D("1000000000"), D("0.00005001")))
        self.assertEqual(broker.round_order("sell", D("1000000001"), D("0.00005"), RULES)[0], D("1000001000"))
        self.assertEqual(broker.fmt(D("1.458427E+9")), "1458427000")
        self.assertEqual(broker.fmt(D("0.00005000")), "0.00005")


class PaperTests(Base):
    def test_paper_buy_then_sell_updates_balance_and_ledger(self):
        self.assertEqual(self.cli(["paper-init", "--idr", "300000"])[0], 0)
        rc, out, _ = self.order(approval())
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual((d["mode"], d["status"], d["qty"], d["fee_idr"]), ("paper", "filled", "0.00005", "172"))
        self.assertEqual(d["paper_balance"]["idr"], str(300000 - 50000 - 172))
        rc, out, _ = self.order(approval(side="sell", idr="20000", amount="0.00002"))
        self.assertEqual(rc, 0)
        bal = json.loads(self.cli(["balance"])[1])
        self.assertEqual(bal["holdings"], {"btc": "0.00003"})
        self.assertEqual(bal["idr"], str(300000 - 50172 + 20000 - 69))
        self.assertEqual(json.loads(self.cli(["orders", "--mode", "paper"])[1])["count"], 2)

    def test_paper_refusals(self):
        self.assertEqual(self.order(approval())[0], 1)                       # no paper balance yet
        self.cli(["paper-init", "--idr", "30000"])
        self.assertEqual(self.order(approval())[0], 1)                       # not enough paper IDR
        self.assertEqual(self.order(approval(side="sell"))[0], 1)            # no coin to sell
        self.assertEqual(self.cli(["paper-init", "--idr", "1"])[0], 1)       # no overwrite without --reset
        self.assertEqual(self.order(approval(idr="5000", amount="0.000005"))[0], 1)   # below Indodax minimum

    def test_kill_switch_blocks_buy(self):
        self.cli(["paper-init", "--idr", "300000"])
        (Path(self.tmp.name) / "HALT").touch()
        self.assertEqual(self.order(approval())[0], 1)


class FakeExchange:
    """Answers /tapi by method. fills: how getOrder reports the order (filled / open / partial)."""

    def __init__(self, fills="filled"):
        self.fills, self.calls, self.cancelled = fills, [], False

    def __call__(self, req, timeout):
        body = req.data.decode()
        q = dict(urllib.parse.parse_qsl(body))
        self.calls.append(q["method"])
        self.last_body, self.last_headers = body, dict(req.header_items())
        if q["method"] == "trade":
            self.qty = q.get("btc")
            ret = {"order_id": 777}
        elif q["method"] == "getOrder":
            remain = {"filled": "0", "open": self.qty, "partial": str(D(self.qty) / 2)}[self.fills]
            st = "filled" if self.fills == "filled" else ("cancelled" if self.cancelled else "open")
            ret = {"order": {"order_id": "777", "price": "1000000000", "order_btc": self.qty,
                             "remain_btc": remain, "status": st}}
        elif q["method"] == "cancelOrder":
            self.cancelled = True
            ret = {"order_id": 777}
        else:
            ret = {}
        return _Resp(json.dumps({"success": 1, "return": ret}).encode())


class BookTests(Base):
    def test_live_init_hard_cap(self):
        self.assertEqual(self.cli(["live-init", "--capital-idr", "150000"])[0], 2)
        self.assertEqual(self.cli(["live-init", "--capital-idr", "100000"])[0], 0)
        self.assertEqual(self.cli(["live-init", "--capital-idr", "100000"])[0], 0)       # idempotent
        self.assertEqual(self.cli(["live-init", "--capital-idr", "90000"])[0], 1)        # no silent change

    def test_book_is_conservative(self):
        self.cli(["live-init", "--capital-idr", "100000"])
        f = broker.FEE
        ledger = [
            {"mode": "live", "pair": "btc_idr", "side": "buy", "price": "1000000", "qty": "0.02",
             "filled_qty": "0.02", "status": "filled"},
            {"mode": "live", "pair": "btc_idr", "side": "sell", "price": "1100000", "qty": "0.01",
             "filled_qty": "0.01", "status": "filled"},
            {"mode": "live", "pair": "eth_idr", "side": "buy", "price": "50000", "qty": "0.1", "status": "unknown"},
            {"mode": "live", "pair": "eth_idr", "side": "buy", "price": "50000", "qty": "0.1", "filled_qty": "0",
             "status": "cancelled"},
            {"mode": "paper", "pair": "btc_idr", "side": "buy", "price": "1", "qty": "999", "filled_qty": "999",
             "status": "filled"}]
        b = broker.live_book(ledger)
        cash = D(100000) - D(20000) * (1 + f) + D(11000) * (1 - f) - D(5000) * (1 + f)
        self.assertEqual(b["idr"], str(cash.quantize(D(1), rounding="ROUND_DOWN")))
        self.assertEqual(b["holdings"], {"btc": broker.fmt((D("0.02") * (1 - f) - D("0.01")).quantize(D("1e-8"), "ROUND_DOWN"))})

    def test_live_order_fenced_by_book(self):
        os.environ["INDODAX_LIVE"] = "true"
        os.environ["INDODAX_API_KEY"], os.environ["INDODAX_API_SECRET"] = FAKE_KEY, FAKE_SECRET
        self.cli(["live-init", "--capital-idr", "30000"])
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("sent")):
            rc, _, err = self.order(approval(idr="40000", amount="0.00004"), "--confirm")
            self.assertEqual(rc, 1)
            self.assertIn("kas buku", err)
            rc, _, err = self.order(approval(side="sell", idr="20000", amount="0.00002"), "--confirm")
        self.assertEqual(rc, 1)
        self.assertIn("buku skills hanya punya", err)


class LiveTests(Base):
    def arm(self, key=True):
        os.environ["INDODAX_LIVE"] = "true"
        if key:
            os.environ["INDODAX_API_KEY"], os.environ["INDODAX_API_SECRET"] = FAKE_KEY, FAKE_SECRET

    def test_live_requires_confirm_and_never_downgrades(self):
        self.arm()
        sent = []
        with mock.patch("urllib.request.urlopen", lambda *a, **k: sent.append(1)):
            rc, _, err = self.order(approval())
        self.assertEqual(rc, 1)
        self.assertIn("--confirm", err)
        self.assertEqual(sent, [])
        self.assertFalse(Path(self.tmp.name, "orders.json").exists())

    def test_confirm_without_live_env_is_paper(self):
        self.cli(["paper-init", "--idr", "300000"])
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            rc, out, _ = self.order(approval(), "--confirm")
        self.assertEqual((rc, json.loads(out)["mode"]), (0, "paper"))

    def test_live_order_signed_settled_and_recorded(self):
        self.arm()
        ex = FakeExchange("filled")
        with mock.patch("urllib.request.urlopen", ex):
            rc, out, err = self.order(approval(), "--confirm")
        self.assertEqual(rc, 0, err)
        self.assertEqual(ex.calls, ["trade", "getOrder"])
        d = json.loads(out)
        self.assertEqual((d["mode"], d["status"], d["order_id"], d["filled_qty"]), ("live", "filled", "777", "0.00005"))
        ledger = Path(self.tmp.name, "orders.json").read_text()
        self.assertNotIn(FAKE_SECRET, out + err + ledger)
        self.assertNotIn(FAKE_KEY, out + err + ledger)

    def test_live_signature(self):
        self.arm()
        ex = FakeExchange("filled")
        with mock.patch("urllib.request.urlopen", ex):
            broker.tapi("getInfo")
        q = dict(urllib.parse.parse_qsl(ex.last_body))
        self.assertEqual(q["method"], "getInfo")
        self.assertEqual(ex.last_headers["Key"], FAKE_KEY)
        self.assertEqual(ex.last_headers["Sign"],
                         hmac.new(FAKE_SECRET.encode(), ex.last_body.encode(), hashlib.sha512).hexdigest())

    def test_unfilled_order_is_cancelled_never_left_resting(self):
        self.arm()
        for fills, status, filled in (("open", "cancelled", "0"), ("partial", "partial", "0.000025")):
            ex = FakeExchange(fills)
            with mock.patch("urllib.request.urlopen", ex), mock.patch.object(broker.time, "sleep"):
                rc, out, err = self.order(approval(idr=str(50000 + len(fills))), "--confirm", "--settle-s", "0.01")
            self.assertEqual(rc, 0, err)
            self.assertIn("cancelOrder", ex.calls)
            d = json.loads(out)
            self.assertEqual((d["status"], d["filled_qty"]), (status, filled))

    def test_live_hard_order_ceiling(self):
        self.arm()
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("sent")):
            rc, _, err = self.order(approval(idr="60000", amount="0.00006"), "--confirm")
        self.assertEqual(rc, 1)
        self.assertIn("batas keras", err)

    def test_live_network_failure_is_unknown_not_retried(self):
        self.arm()
        calls = []

        def boom(req, timeout):
            calls.append(1)
            raise OSError("reset")

        with mock.patch("urllib.request.urlopen", boom):
            rc, _, _ = self.order(approval(), "--confirm")
        self.assertEqual((rc, len(calls)), (3, 1))
        e = json.loads(Path(self.tmp.name, "orders.json").read_text())[-1]
        self.assertEqual(e["status"], "unknown")
        self.assertEqual(self.order(approval(), "--confirm")[0], 1)          # same approval cannot be resent

    def test_live_exchange_rejection_recorded(self):
        self.arm()
        resp = lambda req, timeout: _Resp(json.dumps({"success": 0, "error": "Insufficient balance"}).encode())  # noqa: E731
        with mock.patch("urllib.request.urlopen", resp):
            rc, _, err = self.order(approval(), "--confirm")
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(Path(self.tmp.name, "orders.json").read_text())[-1]["status"], "rejected")

    def test_live_without_keys_refused_before_ledger(self):
        self.arm(key=False)
        self.assertEqual(self.order(approval(), "--confirm")[0], 1)
        self.assertFalse(Path(self.tmp.name, "orders.json").exists())

    def test_method_allowlist(self):
        self.arm()
        for m in ("withdrawCoin", "transferBalance", "trade2"):
            with self.assertRaises(broker.Refused):
                broker.tapi(m)

    def test_live_balance_parsing(self):
        ret = {"balance": {"idr": "123456", "btc": "0.0001", "eth": "0"}, "balance_hold": {"idr": "1000", "btc": "0.5"}}
        self.assertEqual(broker.parse_getinfo(ret), {"source": "live", "idr": "123456", "idr_on_hold": "1000",
                                                     "holdings": {"btc": "0.0001"}, "holdings_on_hold": {"btc": "0.5"}})

    def test_cancel_needs_live_and_confirm(self):
        self.assertEqual(self.cli(["cancel", "--pair", "btc_idr", "--order-id", "1", "--side", "buy", "--confirm"])[0], 1)
        self.arm()
        self.assertEqual(self.cli(["cancel", "--pair", "btc_idr", "--order-id", "1", "--side", "buy"])[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
