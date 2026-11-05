#!/usr/bin/env python3
"""indodax-broker: execute risk-approved orders — PAPER by default, LIVE only when explicitly armed (stdlib only).

    broker.py paper-init --idr 300000 [--hold btc=0.0001] [--reset]
    broker.py balance [--source paper|live] [--pretty]            # JSON usable by indodax-budget/risk --balance
    risk.py check ... > r.json && broker.py order --risk r.json [--confirm]
    broker.py orders [--today] [--mode paper|live] [--limit 20] [--pretty]
    broker.py open [--pair btc_idr]                                # live open orders (read-only)
    broker.py cancel --pair btc_idr --order-id 123 --side buy --confirm   # live only

LIVE requires ALL of: env INDODAX_LIVE=true, env INDODAX_API_KEY + INDODAX_API_SECRET, the --confirm flag on
this very order, and a fresh (<= --max-age s) ALLOW from indodax-risk whose fingerprint matches. Anything
less: with INDODAX_LIVE unset/false the order is recorded as a PAPER trade in orders.json; with
INDODAX_LIVE=true but no --confirm the order is REFUSED (never silently downgraded to paper).
Credentials come only from the environment and are never printed or written to disk.

State ($INDODAX_STATE_DIR, default ~/.indodax-skills): orders.json (ledger), paper_balance.json.
Exit codes: 0 done, 1 refused, 2 bad input, 3 network/API error (live outcome may be unknown — run `open`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "indodax-risk" / "scripts"))
sys.path.insert(0, str(SKILLS / "indodax-market" / "scripts"))
import market  # noqa: E402
import risk  # noqa: E402  (also brings budget)
from budget import D, InputError  # noqa: E402

TAPI = "https://indodax.com/tapi"
RECV_WINDOW_MS = 5000
FEE = risk.LIMITS["fee_pct"] / 100
# Only these private methods can ever be called. Withdrawals/transfers are impossible by construction.
ALLOWED_METHODS = frozenset({"getInfo", "trade", "openOrders", "cancelOrder", "getOrder"})
WIB = risk.WIB


class Refused(Exception):
    """Safety gate said no (exit 1)."""


class ApiError(Exception):
    """Network / exchange failure (exit 3). definite=True: the exchange answered (order surely not placed)."""

    def __init__(self, msg: str, definite: bool = False):
        super().__init__(msg)
        self.definite = definite


# ------------------------------------------------------------------ state files

def state_dir() -> Path:
    return risk.state_dir()


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except ValueError as e:
        raise InputError(f"{path} rusak: {e}") from e


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
    tmp.replace(path)


def ledger_path() -> Path:
    return Path(os.environ.get("INDODAX_ORDERS_FILE") or state_dir() / "orders.json")


def load_ledger() -> list[dict]:
    data = _read(ledger_path(), [])
    if not isinstance(data, list):
        raise InputError("orders.json harus berupa daftar")
    return data


def paper_path() -> Path:
    return state_dir() / "paper_balance.json"


# ------------------------------------------------------------------ live client

def live_armed() -> bool:
    return os.environ.get("INDODAX_LIVE", "").strip().lower() == "true"


def credentials() -> tuple[str, str]:
    k, s = os.environ.get("INDODAX_API_KEY", "").strip(), os.environ.get("INDODAX_API_SECRET", "").strip()
    if not k or not s:
        raise Refused("INDODAX_API_KEY / INDODAX_API_SECRET tidak ada di environment")
    return k, s


def sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha512).hexdigest()


def server_offset_ms() -> int:
    try:
        st = int(market.fetch_json("/api/server_time")["server_time"])
    except (market.MarketError, market.InputError, KeyError, TypeError, ValueError) as e:
        raise ApiError(f"server_time: {e}") from e
    return st - int(time.time() * 1000)


def tapi(method: str, params: dict | None = None, *, offset_ms: int = 0, opener=None) -> dict:
    """One signed POST to /tapi. Never retried (an order may already have reached the exchange)."""
    if method not in ALLOWED_METHODS:
        raise Refused(f"metode {method!r} tidak diizinkan oleh skill ini")
    key, secret = credentials()
    ts = int(time.time() * 1000) + offset_ms - 500          # Indodax rejects ts >= server + 1000 ms
    body = urllib.parse.urlencode({"method": method, "timestamp": ts, "recvWindow": RECV_WINDOW_MS, **(params or {})})
    req = urllib.request.Request(TAPI, data=body.encode(), method="POST", headers={
        "Key": key, "Sign": sign(secret, body), "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "indodax-broker-skill/1.0"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ApiError(f"{method}: HTTP {e.code}", definite=400 <= e.code < 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"{method}: jaringan gagal ({type(e).__name__})") from None
    except ValueError:
        raise ApiError(f"{method}: respons bukan JSON") from None
    if not isinstance(data, dict) or str(data.get("success")) != "1":
        err = data.get("error") if isinstance(data, dict) else None
        code = data.get("error_code") if isinstance(data, dict) else None
        raise ApiError(f"{method}: {err or 'gagal'}" + (f" ({code})" if code else ""), definite=True)
    return data.get("return") or {}


def parse_getinfo(ret: dict) -> dict:
    bal = {k.lower(): D(v) for k, v in (ret.get("balance") or {}).items()}
    hold_ = {k.lower(): D(v) for k, v in (ret.get("balance_hold") or {}).items()}
    idr = bal.pop("idr", Decimal(0))
    return {"source": "live", "idr": str(idr), "idr_on_hold": str(hold_.get("idr", Decimal(0))),
            "holdings": {k: str(v) for k, v in sorted(bal.items()) if v > 0}}


# ------------------------------------------------------------------ pair rules

def pair_rules(pair: str) -> dict:
    """Tick size, quantity step, minimums and trading status from public endpoints."""
    try:
        info = next((p for p in market.fetch_json("/api/pairs") if p.get("ticker_id") == pair), None)
        inc = market.fetch_json("/api/price_increments").get("increments", {})
    except (market.MarketError, market.InputError, AttributeError) as e:
        raise ApiError(f"aturan pair: {e}") from e
    if not info:
        raise InputError(f"pair {pair} tidak ada di Indodax")
    return {"tick": D(inc.get(pair) or "1"), "qty_step": D(str(info.get("quantity_increment") or "0.00000001")),
            "min_idr": D(str(info.get("trade_min_base_currency") or 10000)),
            "min_qty": D(str(info.get("trade_min_traded_currency") or 0)),
            "tradable": not (info.get("is_maintenance") or info.get("is_market_suspended"))}


def fmt(x: Decimal) -> str:
    """Plain decimal string, never scientific notation, no trailing zeros."""
    t = format(x, "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def round_order(side: str, price: Decimal, qty: Decimal, rules: dict) -> tuple[Decimal, Decimal]:
    """Never worse than approved: BUY price down / SELL price up to the tick; quantity always down."""
    tick, step = rules["tick"], rules["qty_step"]
    mode = ROUND_DOWN if side == "buy" else ROUND_CEILING
    p = (price / tick).to_integral_value(rounding=mode) * tick
    q = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    return p, q


# ------------------------------------------------------------------ approval gate (pure)

def verify_approval(r: dict, ledger: list[dict], now: float, max_age: int) -> dict:
    if not isinstance(r, dict) or "decision" not in r:
        raise InputError("--risk harus output JSON dari risk.py check")
    if r["decision"] != "ALLOW":
        raise Refused(f"risk BLOCK: {', '.join(r.get('failed') or []) or 'tanpa alasan'}")
    o = r.get("order") or {}
    if any(k not in o for k in ("pair", "side", "idr", "amount", "price")):
        raise InputError("order di hasil risk tidak lengkap")
    if risk.order_fingerprint(o) != r.get("order_fingerprint"):
        raise Refused("fingerprint tidak cocok — hasil risk telah diubah setelah dicek")
    age = now - float(r.get("checked_at") or 0)
    if not 0 <= age <= max_age:
        raise Refused(f"hasil risk kedaluwarsa ({int(age)} dtk > {max_age} dtk) — jalankan risk.py check lagi")
    if any(e.get("risk_fingerprint") == r["order_fingerprint"] and e.get("status") != "rejected" for e in ledger):
        raise Refused("persetujuan risk ini sudah pernah dipakai (cegah order ganda)")
    if o["side"] not in ("buy", "sell"):
        raise InputError("side tidak valid")
    return o


def paper_fill(bal: dict, coin: str, side: str, price: Decimal, qty: Decimal) -> tuple[dict, dict]:
    """Assume a full fill at the (marketable) limit price. Fees per side as configured."""
    idr = D(bal["idr"])
    hold = {k: D(v) for k, v in bal.get("holdings", {}).items()}
    gross = qty * price
    fee = (gross * FEE).quantize(Decimal(1), rounding=ROUND_CEILING)
    if side == "buy":
        cost = gross + fee
        if cost > idr:
            raise Refused(f"saldo paper kurang: butuh Rp {cost:,.0f}, ada Rp {idr:,.0f}")
        idr -= cost
        hold[coin] = hold.get(coin, Decimal(0)) + qty
        idr_delta = -cost
    else:
        if qty > hold.get(coin, Decimal(0)):
            raise Refused(f"holding paper {coin.upper()} kurang")
        hold[coin] -= qty
        idr_delta = gross - fee
        idr += idr_delta
    new = {**bal, "idr": str(idr.quantize(Decimal(1), rounding=ROUND_DOWN)),
           "holdings": {k: fmt(v) for k, v in sorted(hold.items()) if v > 0}, "updated_at": int(time.time())}
    return new, {"filled_qty": fmt(qty), "fee_idr": str(fee), "idr_delta": str(idr_delta.quantize(Decimal(1)))}


# ------------------------------------------------------------------ commands

def cmd_order(a) -> dict:
    raw = sys.stdin.read() if a.risk == "-" else Path(a.risk).read_text()
    r = json.loads(raw)
    now = time.time()
    ledger = load_ledger()
    o = verify_approval(r, ledger, now, a.max_age)
    pair, side = o["pair"], o["side"]
    coin = risk.budget.coin_of(pair)
    if side == "buy" and risk.halted():
        raise Refused(f"kill switch: {risk.halted()}")
    live = live_armed()
    if live and not a.confirm:
        raise Refused("INDODAX_LIVE=true tetapi --confirm tidak diberikan. Order LIVE butuh konfirmasi eksplisit "
                      f"per order: {side.upper()} {o['amount']} {coin.upper()} @ {o['price']} (≈ Rp {o['idr']})")
    rules = pair_rules(pair)
    if not rules["tradable"]:
        raise Refused(f"{pair} sedang maintenance/suspend")
    price, qty = round_order(side, D(o["price"]), D(o["amount"]), rules)
    if qty <= 0 or qty < rules["min_qty"] or qty * price < rules["min_idr"]:
        raise Refused(f"di bawah minimum Indodax setelah pembulatan ({qty} {coin.upper()} ≈ Rp {qty * price:,.0f})")
    coid = f"sk-{int(now)}-{secrets.token_hex(4)}"
    entry = {"ts": int(now), "date_wib": dt.datetime.fromtimestamp(now, WIB).date().isoformat(),
             "mode": "live" if live else "paper", "pair": pair, "side": side, "price": fmt(price), "qty": fmt(qty),
             "idr": str((price * qty).quantize(Decimal(1))), "stop_pct": o.get("stop_pct"),
             "client_order_id": coid, "risk_fingerprint": r["order_fingerprint"], "status": "submitting"}
    if not live:
        bal = _read(paper_path(), None)
        if bal is None:
            raise Refused("saldo paper belum ada — jalankan: broker.py paper-init --idr <saldo>")
        new_bal, fill = paper_fill(bal, coin, side, price, qty)
        entry.update(status="filled", **fill)
        _write(paper_path(), new_bal)
        _write(ledger_path(), ledger + [entry])
        return {**entry, "paper_balance": new_bal,
                "note": "PAPER — tidak ada order yang dikirim ke Indodax (set INDODAX_LIVE=true + --confirm untuk live)"}
    credentials()                                            # fail before writing anything
    _write(ledger_path(), ledger + [entry])                  # record intent BEFORE sending
    params = {"pair": pair, "type": side, "price": fmt(price), "order_type": "limit",
              "time_in_force": "GTC", "client_order_id": coid, coin: fmt(qty)}
    try:
        ret = tapi("trade", params, offset_ms=server_offset_ms())
    except ApiError as e:
        entry["status"] = "rejected" if e.definite else "unknown"
        entry["error"] = str(e)
        _write(ledger_path(), ledger + [entry])
        raise
    entry.update(status="submitted", order_id=str(ret.get("order_id", "")),
                 response={k: v for k, v in ret.items() if not k.startswith("balance")})
    _write(ledger_path(), ledger + [entry])
    return {**entry, "note": "LIVE — order dikirim ke Indodax; cek status dengan broker.py open"}


def cmd_balance(a) -> dict:
    src = a.source or ("live" if live_armed() else "paper")
    if src == "live":
        return parse_getinfo(tapi("getInfo", offset_ms=server_offset_ms()))
    bal = _read(paper_path(), None)
    if bal is None:
        raise Refused("saldo paper belum ada — jalankan: broker.py paper-init --idr <saldo>")
    return {"source": "paper", **bal}


def cmd_paper_init(a) -> dict:
    if paper_path().exists() and not a.reset:
        raise Refused(f"{paper_path()} sudah ada — pakai --reset untuk menimpa")
    idr = D(a.idr, "--idr")
    if idr < 0:
        raise InputError("--idr harus >= 0")
    hold = risk.budget.parse_kv(a.hold, "--hold")
    bal = {"idr": str(idr), "holdings": {k: str(v) for k, v in hold.items() if v > 0},
           "created_at": int(time.time())}
    _write(paper_path(), bal)
    return {"source": "paper", **bal}


def cmd_orders(a) -> dict:
    rows = load_ledger()
    if a.mode:
        rows = [r for r in rows if r.get("mode") == a.mode]
    if a.today:
        today = dt.datetime.now(WIB).date().isoformat()
        rows = [r for r in rows if r.get("date_wib") == today]
    return {"ledger": str(ledger_path()), "count": len(rows), "orders": rows[-a.limit:]}


def cmd_open(a) -> dict:
    params = {"pair": a.pair} if a.pair else {}
    if a.pair:
        risk.budget.coin_of(a.pair)
    ret = tapi("openOrders", params, offset_ms=server_offset_ms())
    return {"source": "live", "orders": ret.get("orders", {})}


def cmd_cancel(a) -> dict:
    if not live_armed():
        raise Refused("cancel hanya untuk order live (INDODAX_LIVE=true); order paper langsung terisi")
    if not a.confirm:
        raise Refused("cancel live butuh --confirm")
    coin = risk.budget.coin_of(a.pair)
    if not a.order_id.isdigit():
        raise InputError("--order-id harus angka")
    ret = tapi("cancelOrder", {"pair": f"{coin}_idr", "order_id": a.order_id, "type": a.side, "order_type": "limit"},
               offset_ms=server_offset_ms())
    return {"source": "live", "cancelled": a.order_id, "response": ret}


def _pretty(cmd: str, d: dict) -> str:
    if cmd == "order":
        return (f"[{d['mode'].upper()}] {d['side'].upper()} {d['qty']} {d['pair'].split('_')[0].upper()} @ {d['price']} "
                f"(≈ Rp {int(D(d['idr'])):,}) → {d['status']}" + (f" · fee Rp {d['fee_idr']}" if d.get("fee_idr") else "")
                + f"\n{d['note']}")
    if cmd in ("balance", "paper-init"):
        h = ", ".join(f"{k.upper()} {v}" for k, v in d.get("holdings", {}).items()) or "-"
        return f"[{d['source'].upper()}] IDR Rp {int(D(d['idr'])):,} · koin: {h}"
    if cmd == "orders":
        lines = [f"{d['count']} order di {d['ledger']}"]
        for r in d["orders"]:
            t = dt.datetime.fromtimestamp(r["ts"], WIB).strftime("%m-%d %H:%M")
            lines.append(f"  {t} [{r['mode']}] {r['side']:4s} {r['pair']:9s} {r['qty']} @ {r['price']} → {r['status']}")
        return "\n".join(lines)
    return json.dumps(d, indent=1, ensure_ascii=False, default=str)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Indodax broker: paper by default, live only when explicitly armed")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("order")
    o.add_argument("--risk", required=True, help="output risk.py check (file atau '-')")
    o.add_argument("--confirm", action="store_true", help="konfirmasi eksplisit untuk order LIVE ini")
    o.add_argument("--max-age", type=int, default=60, help="umur maksimum hasil risk (detik, maks 300)")
    b = sub.add_parser("balance")
    b.add_argument("--source", choices=["paper", "live"])
    pi = sub.add_parser("paper-init")
    pi.add_argument("--idr", required=True)
    pi.add_argument("--hold")
    pi.add_argument("--reset", action="store_true")
    ls = sub.add_parser("orders")
    ls.add_argument("--today", action="store_true")
    ls.add_argument("--mode", choices=["paper", "live"])
    ls.add_argument("--limit", type=int, default=20)
    op = sub.add_parser("open")
    op.add_argument("--pair")
    ca = sub.add_parser("cancel")
    ca.add_argument("--pair", required=True)
    ca.add_argument("--order-id", required=True)
    ca.add_argument("--side", choices=["buy", "sell"], required=True)
    ca.add_argument("--confirm", action="store_true")
    for p in (o, b, pi, ls, op, ca):
        p.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    fn = {"order": cmd_order, "balance": cmd_balance, "paper-init": cmd_paper_init, "orders": cmd_orders,
          "open": cmd_open, "cancel": cmd_cancel}[a.cmd]
    try:
        if a.cmd == "order" and not 1 <= a.max_age <= 300:
            raise InputError("--max-age 1..300")
        d = fn(a)
    except Refused as e:
        print(json.dumps({"refused": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except ApiError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (InputError, json.JSONDecodeError, OSError, ValueError, KeyError, ArithmeticError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(_pretty(a.cmd, d) if a.pretty else json.dumps(d, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
