#!/usr/bin/env python3
"""indodax-market: read-only public market data from Indodax (standard library only).

    market.py ticker btc_idr [--pretty]
    market.py orderbook btc_idr [--depth 10] [--pretty]
    market.py trades btc_idr [--limit 50] [--pretty]
    market.py pairs [--quote idr] [--pretty]

Prints JSON. Exit codes: 0 ok, 2 bad input / unknown pair, 3 network or API failure after retries.
No API key is ever used or accepted.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

BASE = "https://indodax.com"
USER_AGENT = "indodax-market-skill/1.0"
TIMEOUT_S = 10
RETRIES = 3


class MarketError(Exception):
    """Network/API failure (exit 3)."""


class InputError(Exception):
    """Bad pair or arguments (exit 2)."""


# ------------------------------------------------------------------ helpers

def pair_id(pair: str) -> str:
    """'btc_idr' | 'BTC/IDR' | 'btcidr' | 'BTC-IDR' -> 'btcidr' (Indodax id used by ticker/depth/trades)."""
    p = (pair or "").strip().lower()
    p = re.sub(r"[\s/_\-]", "", p)
    if not re.fullmatch(r"[a-z0-9]{2,20}", p):
        raise InputError(f"format pair tidak valid: {pair!r} (contoh: btc_idr)")
    return p


def ticker_name(pid: str) -> str:
    for quote in ("idr", "usdt", "btc"):
        if pid.endswith(quote) and len(pid) > len(quote):
            return f"{pid[:-len(quote)]}_{quote}"
    return pid


def dec(x) -> Decimal | None:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


def s(x: Decimal | None) -> str | None:
    """Decimal -> plain string (no exponent), None stays None."""
    if x is None:
        return None
    t = format(x, "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def fetch_json(path: str, params: dict | None = None, retries: int = RETRIES, sleep=time.sleep):
    """GET BASE+path with a cache-busting param; retries network errors and 5xx/429 with backoff."""
    q = dict(params or {})
    q["_"] = str(time.time_ns())            # Indodax public endpoints are cached by Cloudflare
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                               "Cache-Control": "no-cache"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise InputError(f"tidak ditemukan: {path} (pair tidak ada?)") from e
            last = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = type(e).__name__
        except json.JSONDecodeError as e:
            last = "respons bukan JSON"
            raise MarketError(f"{path}: {last}") from e
        if attempt < retries:
            sleep(min(0.5 * 2 ** attempt, 8) * (0.5 + random.random() / 2))
    raise MarketError(f"{path}: gagal setelah {retries + 1} percobaan ({last})")


# ------------------------------------------------------------------ commands

def parse_ticker(pid: str, data: dict, now: float | None = None) -> dict:
    t = (data or {}).get("ticker")
    if not isinstance(t, dict) or "last" not in t:
        raise InputError(f"pair {ticker_name(pid)} tidak dikenal atau respons tidak berisi ticker")
    base = ticker_name(pid).split("_")[0]
    bid, ask = dec(t.get("buy")), dec(t.get("sell"))
    mid = (bid + ask) / 2 if bid and ask else None
    server_time = int(t.get("server_time") or 0) or None
    now = time.time() if now is None else now
    return {
        "pair": ticker_name(pid),
        "last": s(dec(t.get("last"))), "bid": s(bid), "ask": s(ask),
        "spread_pct": round(float((ask - bid) / mid * 100), 4) if mid else None,
        "high": s(dec(t.get("high"))), "low": s(dec(t.get("low"))),
        "vol_base": s(dec(t.get(f"vol_{base}"))), "vol_idr": s(dec(t.get("vol_idr"))),
        "server_time": server_time,
        "age_s": round(now - server_time, 1) if server_time else None,
    }


def ticker(pair: str) -> dict:
    pid = pair_id(pair)
    return parse_ticker(pid, fetch_json(f"/api/ticker/{pid}"))


def parse_orderbook(pid: str, data: dict, depth: int) -> dict:
    if not isinstance(data, dict) or "buy" not in data or "sell" not in data:
        raise InputError(f"orderbook {ticker_name(pid)} tidak tersedia")
    bids = [(dec(p), dec(q)) for p, q in data["buy"][:depth]]
    asks = [(dec(p), dec(q)) for p, q in data["sell"][:depth]]
    bb, ba = (bids[0][0] if bids else None), (asks[0][0] if asks else None)
    mid = (bb + ba) / 2 if bb and ba else None
    return {
        "pair": ticker_name(pid),
        "best_bid": s(bb), "best_ask": s(ba),
        "spread_pct": round(float((ba - bb) / mid * 100), 4) if mid else None,
        "bids": [[s(p), s(q)] for p, q in bids], "asks": [[s(p), s(q)] for p, q in asks],
        "bid_depth_idr": s(sum((p * q for p, q in bids), Decimal(0))),
        "ask_depth_idr": s(sum((p * q for p, q in asks), Decimal(0))),
    }


def orderbook(pair: str, depth: int = 10) -> dict:
    pid = pair_id(pair)
    return parse_orderbook(pid, fetch_json(f"/api/depth/{pid}"), depth)


def parse_trades(pid: str, data, limit: int) -> dict:
    if not isinstance(data, list):
        raise InputError(f"trade history {ticker_name(pid)} tidak tersedia")
    rows = sorted(data, key=lambda r: int(r.get("date", 0)), reverse=True)[:limit]
    return {
        "pair": ticker_name(pid),
        "trades": [{"time": int(r["date"]), "price": s(dec(r["price"])), "amount": s(dec(r["amount"])),
                    "side": r.get("type"), "tid": str(r.get("tid"))} for r in rows],
    }


def trades(pair: str, limit: int = 50) -> dict:
    pid = pair_id(pair)
    return parse_trades(pid, fetch_json(f"/api/trades/{pid}"), limit)


def pairs(quote: str | None = None) -> dict:
    data = fetch_json("/api/pairs")
    if not isinstance(data, list):
        raise MarketError("/api/pairs: format tidak dikenal")
    out = []
    for p in data:
        tid = p.get("ticker_id") or ""
        if quote and not tid.endswith("_" + quote.lower()):
            continue
        out.append({"pair": tid, "id": p.get("id"), "min_idr": p.get("trade_min_base_currency"),
                     "min_coin": p.get("trade_min_traded_currency"),
                     "maintenance": bool(int(p.get("is_maintenance") or 0))})
    return {"count": len(out), "pairs": out}


# ------------------------------------------------------------------ cli

def _pretty(cmd: str, d: dict) -> str:
    if cmd == "ticker":
        return (f"{d['pair'].upper()}  last {d['last']}  bid {d['bid']}  ask {d['ask']}  spread {d['spread_pct']}%\n"
                f"24j high {d['high']}  low {d['low']}  vol {d['vol_base']} ({d['vol_idr']} IDR)  "
                f"umur data {d['age_s']} dtk")
    if cmd == "orderbook":
        lines = [f"{d['pair'].upper()}  spread {d['spread_pct']}%   (harga  |  jumlah)"]
        for (bp, bq), (ap, aq) in zip(d["bids"], d["asks"]):
            lines.append(f"BID {bp:>18} | {bq:<14}   ASK {ap:>18} | {aq}")
        return "\n".join(lines)
    return json.dumps(d, indent=2, ensure_ascii=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Indodax public market data (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("ticker"); t.add_argument("pair")
    o = sub.add_parser("orderbook"); o.add_argument("pair"); o.add_argument("--depth", type=int, default=10)
    tr = sub.add_parser("trades"); tr.add_argument("pair"); tr.add_argument("--limit", type=int, default=50)
    pa = sub.add_parser("pairs"); pa.add_argument("--quote", default=None)
    for p in (t, o, tr, pa):
        p.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ticker":
            d = ticker(a.pair)
        elif a.cmd == "orderbook":
            d = orderbook(a.pair, max(1, min(a.depth, 150)))
        elif a.cmd == "trades":
            d = trades(a.pair, max(1, min(a.limit, 1000)))
        else:
            d = pairs(a.quote)
    except InputError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    except MarketError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 3
    print(_pretty(a.cmd, d) if a.pretty else json.dumps(d, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
