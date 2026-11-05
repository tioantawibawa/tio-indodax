#!/usr/bin/env python3
"""indodax-risk: check an order against hard limits before it may reach the broker (stdlib only).

    risk.py check btc_idr --side buy --idr-order 50000 --stop-pct 5 --balance balance.json [--pretty]
    budget.py size btc_idr --balance b.json | risk.py check --order - --balance b.json --stop-pct 5
    risk.py status --balance balance.json [--pretty]       # daily P&L vs limit, kill switch

Checks (BUY): kill switch, stop-loss present, max loss per trade, max exposure per coin, max total
exposure, daily loss limit, enough IDR, minimum order, price vs market.  SELL only reduces risk, so
it is checked for holding/minimum/price only and is allowed even when the daily limit is hit.

Daily loss = equity now vs equity at the first check of the day (WIB), stored in
$INDODAX_STATE_DIR/risk_state.json (default ~/.indodax-skills). Kill switch: env INDODAX_HALT=1 or a file
named HALT in that directory blocks every BUY.

Output decision ALLOW/BLOCK. This skill never places orders. Exit codes: 0 ALLOW, 1 BLOCK, 2 bad input,
3 market data unavailable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

BUDGET_DIR = Path(__file__).resolve().parents[2] / "indodax-budget" / "scripts"
sys.path.insert(0, str(BUDGET_DIR))
import budget  # noqa: E402  sibling skill: parsing, pricing, equity
from budget import D, InputError, MarketDown  # noqa: E402

WIB = dt.timezone(dt.timedelta(hours=7))
LIMITS = {
    "max_loss_per_trade_pct": Decimal("1"),   # loss at the stop (incl. fees) as % of equity
    "max_coin_pct": Decimal("25"),            # coin value after the order as % of equity
    "max_total_exposure_pct": Decimal("80"),  # all coins after the order as % of equity
    "daily_loss_limit_pct": Decimal("3"),     # no new BUYs once equity is this far below day start
    "max_price_dev_pct": Decimal("2"),        # order price vs market ask/bid
    "fee_pct": budget.DEFAULTS["fee_pct"],
    "min_order_idr": budget.DEFAULTS["min_order_idr"],
}


def state_dir() -> Path:
    return Path(os.environ.get("INDODAX_STATE_DIR") or Path.home() / ".indodax-skills")


def halted() -> str | None:
    if os.environ.get("INDODAX_HALT", "").strip().lower() in ("1", "true", "yes"):
        return "env INDODAX_HALT aktif"
    if (state_dir() / "HALT").exists():
        return f"file {state_dir() / 'HALT'} ada"
    return None


def day_start(equity: Decimal, now: float, override: Decimal | None = None, record: bool = True) -> tuple[str, Decimal]:
    """Equity at the first check of today's WIB date (recorded on first call)."""
    today = dt.datetime.fromtimestamp(now, WIB).date().isoformat()
    if override is not None:
        return today, override
    path = state_dir() / "risk_state.json"
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    if st.get("date") == today and st.get("start_equity_idr"):
        return today, D(st["start_equity_idr"])
    if record:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": today, "start_equity_idr": str(equity), "recorded_at": int(now)}))
        tmp.replace(path)
    return today, equity


def order_fingerprint(o: dict) -> str:
    key = "|".join(str(o[k]) for k in ("pair", "side", "idr", "amount", "price"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _chk(name, ok, value=None, limit=None, detail=""):
    return {"name": name, "ok": bool(ok), "value": value, "limit": limit, "detail": detail}


def evaluate(order: dict, idr: Decimal, hold: dict, px: dict, lim: dict, start_equity: Decimal,
             halt: str | None = None) -> dict:
    """order: {pair, coin, side, idr (IDR value), amount (coin), price, stop_pct|None}. Pure."""
    coin, side = order["coin"], order["side"]
    eq, values = budget.equity_of(idr, hold, px)
    fee = lim["fee_pct"] / 100
    val, price = order["idr"], order["price"]
    mkt = px[coin]["ask"] if side == "buy" else px[coin]["bid"]
    dev = abs(price - mkt) / mkt * 100
    raw = (lambda x: x / eq * 100) if eq > 0 else (lambda x: Decimal(0))
    pct = lambda x: round(raw(x), 2)  # noqa: E731  display only; limits compare raw values
    day_pnl = eq - start_equity
    day_raw = day_pnl / start_equity * 100 if start_equity > 0 else Decimal(0)
    day_pct = round(day_raw, 2)
    checks = [
        _chk("min_order", val >= lim["min_order_idr"], val, lim["min_order_idr"], "nilai order (IDR)"),
        _chk("price_vs_market", dev <= lim["max_price_dev_pct"], round(dev, 3), lim["max_price_dev_pct"],
             f"harga order {price} vs pasar {mkt} (%)"),
    ]
    if side == "buy":
        stop = order.get("stop_pct")
        loss = val * (stop / 100 + 2 * fee) if stop is not None else None
        coin_after = values.get(coin, Decimal(0)) + val
        total_after = sum(values.values(), Decimal(0)) + val
        checks += [
            _chk("kill_switch", halt is None, None, None, halt or "tidak aktif"),
            _chk("stop_loss_set", stop is not None and 0 < stop < 100, stop, None, "BUY wajib punya stop (%)"),
            _chk("max_loss_per_trade", loss is not None and raw(loss) <= lim["max_loss_per_trade_pct"],
                 pct(loss) if loss is not None else None, lim["max_loss_per_trade_pct"],
                 f"rugi di stop ≈ Rp {int(loss):,} (% equity)" if loss is not None else "tanpa stop tidak bisa dihitung"),
            _chk("max_coin_exposure", raw(coin_after) <= lim["max_coin_pct"], pct(coin_after), lim["max_coin_pct"],
                 f"{coin.upper()} setelah order (% equity)"),
            _chk("max_total_exposure", raw(total_after) <= lim["max_total_exposure_pct"], pct(total_after),
                 lim["max_total_exposure_pct"], "semua koin setelah order (% equity)"),
            _chk("daily_loss_limit", day_raw > -lim["daily_loss_limit_pct"], day_pct, -lim["daily_loss_limit_pct"],
                 f"P&L hari ini Rp {int(day_pnl):,} (%)"),
            _chk("enough_idr", val <= idr, int(idr), None, "kas IDR cukup"),
        ]
    else:
        have = hold.get(coin, Decimal(0))
        checks.append(_chk("enough_coin", order["amount"] <= have, str(order["amount"]), str(have),
                           f"holding {coin.upper()}"))
    failed = [c["name"] for c in checks if not c["ok"]]
    pub = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in order.items() if k != "coin"}
    return {"decision": "BLOCK" if failed else "ALLOW", "failed": failed, "order": pub,
            "order_fingerprint": order_fingerprint(order),
            "equity_idr": int(eq), "day_start_equity_idr": int(start_equity), "day_pnl_pct": day_pct,
            "checks": checks, "limits": lim,
            "note": "hasil cek risiko, bukan order — hanya ALLOW yang boleh diteruskan ke indodax-broker"}


# ------------------------------------------------------------------ CLI

def load_order(src: str | None) -> dict | None:
    if not src:
        return None
    o = json.loads(sys.stdin.read() if src == "-" else Path(src).read_text())
    if not isinstance(o, dict) or "pair" not in o:
        raise InputError("--order harus JSON dari budget.py size (butuh pair, side, order_idr, est_amount, price)")
    if o.get("ok") is False:
        raise InputError(f"usulan budget tidak ok: {o.get('reason')}")
    return o


def build_order(a, px, o: dict | None) -> dict:
    if o is not None:
        pair, side = o["pair"], o.get("side", "buy")
        idr = D(o.get("order_idr", 0), "order_idr")
        amount = D(o.get("est_amount", 0), "est_amount")
        price = D(o["price"], "price") if o.get("price") else None
    else:
        if not a.pair:
            raise InputError("beri pair atau --order")
        pair, side = a.pair, a.side
        idr = D(a.idr_order, "--idr-order") if a.idr_order else None
        amount = D(a.amount, "--amount") if a.amount else None
        price = D(a.price_order, "--price-order") if a.price_order else None
    coin = budget.coin_of(pair)
    if side not in ("buy", "sell"):
        raise InputError("side harus buy atau sell")
    if coin not in px:
        raise InputError(f"tidak ada harga untuk {coin}")
    price = price or (px[coin]["ask"] if side == "buy" else px[coin]["bid"])
    if price <= 0:
        raise InputError("harga harus > 0")
    fee = LIMITS["fee_pct"] / 100
    if idr is None and amount is None:
        raise InputError("beri --idr-order atau --amount")
    if idr is None:
        idr = amount * price
    if amount is None:
        amount = idr * (1 - fee) / price if side == "buy" else idr / price
    if idr <= 0 or amount <= 0:
        raise InputError("nilai order harus > 0")
    stop = D(a.stop_pct, "--stop-pct") if a.stop_pct else None
    if a.stop_price:
        sp = D(a.stop_price, "--stop-price")
        if not 0 < sp < price:
            raise InputError("--stop-price harus di bawah harga beli")
        stop = round((price - sp) / price * 100, 4)
    return {"pair": f"{coin}_idr", "coin": coin, "side": side, "idr": idr.quantize(Decimal(1)),
            "amount": amount.quantize(Decimal("0.00000001")), "price": price, "stop_pct": stop}


def _pretty(d: dict) -> str:
    if "checks" not in d:
        return (f"Equity Rp {d['equity_idr']:,} · awal hari ({d['date']}) Rp {d['day_start_equity_idr']:,} · "
                f"P&L {d['day_pnl_pct']}% (batas −{d['daily_loss_limit_pct']}%) · BUY "
                + ("DIBLOKIR" if d["buy_blocked"] else "boleh") + (f" · HALT: {d['halt']}" if d["halt"] else ""))
    o = d["order"]
    lines = [f"{d['decision']} — {o['side'].upper()} {o['pair'].upper()} Rp {int(D(o['idr'])):,} @ {o['price']}"]
    for c in d["checks"]:
        lim = f" (batas {c['limit']})" if c["limit"] is not None else ""
        val = f": {c['value']}" if c["value"] is not None else ""
        lines.append(f"  {'✅' if c['ok'] else '❌'} {c['name']}{val}{lim} — {c['detail']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-trade risk checks for Indodax (no orders)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("pair", nargs="?")
    c.add_argument("--order", help="JSON budget.py size (file atau '-')")
    c.add_argument("--side", choices=["buy", "sell"], default="buy")
    c.add_argument("--idr-order", help="nilai order dalam IDR")
    c.add_argument("--amount", help="jumlah koin")
    c.add_argument("--price-order", help="harga limit order (default harga pasar)")
    c.add_argument("--stop-pct")
    c.add_argument("--stop-price")
    st = sub.add_parser("status")
    for x in (c, st):
        x.add_argument("--balance", help="file balance JSON (format sama dengan indodax-budget)")
        x.add_argument("--idr")
        x.add_argument("--hold")
        x.add_argument("--price", help="harga pasar manual, mis. btc=1460000000")
        x.add_argument("--day-start-equity", help="override equity awal hari (IDR)")
        for k in LIMITS:
            x.add_argument("--" + k.replace("_", "-"))
        x.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    try:
        lim = dict(LIMITS)
        for k in lim:
            v = getattr(a, k)
            if v is not None:
                lim[k] = D(v, k)
        if any(v < 0 for v in lim.values()) or lim["max_coin_pct"] > 100 or lim["max_total_exposure_pct"] > 100:
            raise InputError("batas di luar rentang wajar")
        if a.balance == "-" and getattr(a, "order", None) == "-":
            raise InputError("--balance dan --order tidak boleh sama-sama stdin")
        if a.balance:
            raw = sys.stdin.read() if a.balance == "-" else Path(a.balance).read_text()
            idr, hold = budget.parse_balance(json.loads(raw))
        elif a.idr is not None:
            idr, hold = budget.parse_balance({"idr": a.idr, "holdings": {k: str(v) for k, v in budget.parse_kv(a.hold, "--hold").items()}})
        else:
            raise InputError("saldo wajib: --balance atau --idr (risk tidak boleh menebak saldo)")
        manual = budget.parse_kv(a.price, "--price")
        px = budget.manual_prices(manual)
        coins = set(hold)
        order_obj = load_order(a.order) if a.cmd == "check" else None
        if a.cmd == "check":
            hint = order_obj["pair"] if order_obj else a.pair
            if hint:
                coins.add(budget.coin_of(hint))
        need = coins - set(px)
        if need:
            px.update(budget.fetch_prices(need))
        eq, _ = budget.equity_of(idr, hold, px)
        if eq <= 0:
            raise InputError("equity 0")
        override = D(a.day_start_equity, "--day-start-equity") if a.day_start_equity else None
        now = time.time()
        today, start = day_start(eq, now, override)
        halt = halted()
        if a.cmd == "status":
            day_pct = round((eq - start) / start * 100, 2)
            d = {"date": today, "equity_idr": int(eq), "day_start_equity_idr": int(start), "day_pnl_pct": day_pct,
                 "daily_loss_limit_pct": lim["daily_loss_limit_pct"], "halt": halt,
                 "buy_blocked": bool(halt) or day_pct <= -lim["daily_loss_limit_pct"], "limits": lim}
            print(_pretty(d) if a.pretty else json.dumps(d, default=str, ensure_ascii=False))
            return 0
        order = build_order(a, px, order_obj)
        d = evaluate(order, idr, hold, px, lim, start, halt)
        d["checked_at"] = int(now)
    except MarketDown as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 3
    except (InputError, json.JSONDecodeError, OSError, ValueError, KeyError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(_pretty(d) if a.pretty else json.dumps(d, default=str, ensure_ascii=False))
    return 0 if d["decision"] == "ALLOW" else 1


if __name__ == "__main__":
    sys.exit(main())
