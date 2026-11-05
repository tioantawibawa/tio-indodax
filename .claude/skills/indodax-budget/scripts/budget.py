#!/usr/bin/env python3
"""indodax-budget: position sizing from your IDR balance with a cash buffer and a per-coin cap (stdlib only).

    budget.py size btc_idr --balance balance.json [--want 100000 | --pct 10] [--stop-pct 5] [--pretty]
    budget.py size eth_idr --idr 300000 --hold btc=0.0001 --price btc=1460000000,eth=52000000
    budget.py size btc_idr --side sell --balance - --pct 50          # balance JSON on stdin
    budget.py plan --balance balance.json [--pairs btc_idr,eth_idr]   # headroom per coin

Balance JSON: {"idr": 300000, "holdings": {"btc": "0.0001"}} or Indodax getInfo style
{"balance": {"idr": "300000", "btc": "0.0001"}} (both accepted; keys must be coin symbols).
Prices come from indodax-market (ask to buy, bid to sell, last to value holdings) unless --price is given.

Output is a SIZING PROPOSAL only — it never places orders. Next step: indodax-risk, then indodax-broker.
Exit codes: 0 ok (also when the size is 0 — read `ok`/`reason`), 2 bad input, 3 market data unavailable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path

MARKET_DIR = Path(__file__).resolve().parents[2] / "indodax-market" / "scripts"

DEFAULTS = {
    "cash_buffer_pct": Decimal("20"),     # always keep this share of equity in IDR
    "max_coin_pct": Decimal("25"),        # max value of one coin as share of equity
    "risk_pct": Decimal("1"),             # with --stop-pct: lose at most this share of equity at the stop
    "fee_pct": Decimal("0.3422"),         # taker 0.2 + tax 0.12 + clearing 0.0222 (Indodax, per side)
    "min_order_idr": Decimal("10000"),    # Indodax minimum order value for IDR pairs
}


class InputError(Exception):
    pass


class MarketDown(Exception):
    pass


# ------------------------------------------------------------------ parsing

def D(x, what="angka") -> Decimal:
    try:
        v = Decimal(str(x).strip())
    except (InvalidOperation, AttributeError) as e:
        raise InputError(f"{what} tidak valid: {x!r}") from e
    if not v.is_finite():
        raise InputError(f"{what} tidak valid: {x!r}")
    return v


def coin_of(pair: str) -> str:
    p = re.sub(r"[\s/_\-]", "", (pair or "").strip().lower())
    if not re.fullmatch(r"[a-z0-9]{1,16}idr", p) or p == "idr":
        raise InputError(f"hanya pair IDR yang didukung, mis. btc_idr (dapat {pair!r})")
    return p[:-3]


def parse_kv(text: str | None, what: str) -> dict[str, Decimal]:
    out = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise InputError(f"{what}: format koin=nilai, dapat {part!r}")
        k, v = part.split("=", 1)
        k = k.strip().lower().removesuffix("_idr")
        if not re.fullmatch(r"[a-z0-9]{1,16}", k):
            raise InputError(f"{what}: simbol koin tidak valid {k!r}")
        out[k] = D(v, what)
    return out


def parse_balance(data: dict) -> tuple[Decimal, dict[str, Decimal]]:
    if not isinstance(data, dict):
        raise InputError("balance harus objek JSON")
    if isinstance(data.get("balance"), dict):                       # Indodax getInfo style
        b = {k.lower(): D(v, f"saldo {k}") for k, v in data["balance"].items()}
        idr = b.pop("idr", Decimal(0))
        hold = b
    elif "idr" in data:
        idr = D(data["idr"], "saldo idr")
        hold = {k.lower(): D(v, f"saldo {k}") for k, v in (data.get("holdings") or {}).items()}
    else:
        raise InputError('balance JSON butuh "idr" (+ "holdings") atau "balance" ala getInfo')
    if idr < 0 or any(v < 0 for v in hold.values()):
        raise InputError("saldo tidak boleh negatif")
    return idr, {k: v for k, v in hold.items() if v > 0}


# ------------------------------------------------------------------ pricing

def fetch_prices(coins) -> dict[str, dict[str, Decimal]]:
    sys.path.insert(0, str(MARKET_DIR))
    import market  # sibling skill
    out = {}
    for c in sorted(set(coins)):
        try:
            t = market.ticker(f"{c}_idr")
        except market.InputError as e:
            raise InputError(str(e)) from e
        except market.MarketError as e:
            raise MarketDown(str(e)) from e
        if t.get("age_s") is not None and t["age_s"] > 120:
            raise MarketDown(f"harga {c}_idr basi ({t['age_s']} dtk)")
        out[c] = {k: D(t[k]) for k in ("last", "bid", "ask")}
    return out


def manual_prices(prices: dict[str, Decimal]) -> dict[str, dict[str, Decimal]]:
    return {c: {"last": p, "bid": p, "ask": p} for c, p in prices.items()}


# ------------------------------------------------------------------ sizing (pure)

def _idr(x: Decimal) -> Decimal:
    return x.quantize(Decimal(1), rounding=ROUND_DOWN)


def equity_of(idr: Decimal, hold: dict[str, Decimal], px: dict) -> tuple[Decimal, dict[str, Decimal]]:
    missing = [c for c in hold if c not in px]
    if missing:
        raise InputError(f"tidak ada harga untuk: {', '.join(missing)} (pakai --price)")
    values = {c: q * px[c]["last"] for c, q in hold.items()}
    return idr + sum(values.values(), Decimal(0)), values


def size(coin: str, side: str, idr: Decimal, hold: dict, px: dict, cfg: dict,
         want_idr: Decimal | None = None, want_pct: Decimal | None = None,
         stop_pct: Decimal | None = None) -> dict:
    eq, values = equity_of(idr, hold, px)
    if coin not in px:
        raise InputError(f"tidak ada harga untuk {coin}")
    pos_val = values.get(coin, Decimal(0))
    fee = cfg["fee_pct"] / 100
    limits: dict[str, Decimal] = {}
    base = {"pair": f"{coin}_idr", "side": side, "equity_idr": _idr(eq), "idr_free": _idr(idr),
            "position_idr": _idr(pos_val), "position_pct": round(pos_val / eq * 100, 2) if eq else Decimal(0)}

    if side == "buy":
        reserve = eq * cfg["cash_buffer_pct"] / 100
        cap = eq * cfg["max_coin_pct"] / 100
        limits["deployable"] = max(Decimal(0), idr - reserve)              # cash above the buffer
        limits["coin_cap"] = max(Decimal(0), cap - pos_val)                # headroom under the cap
        if want_idr is not None:
            limits["requested"] = want_idr
        if want_pct is not None:
            limits["requested"] = eq * want_pct / 100
        if stop_pct is not None:                                           # loss at stop incl. both fees
            per_idr = stop_pct / 100 + 2 * fee
            limits["risk_per_trade"] = eq * cfg["risk_pct"] / 100 / per_idr
        order = _idr(min(limits.values()))
        price = px[coin]["ask"]
        amount = (order * (1 - fee) / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN) if order else Decimal(0)
        extra = {"reserve_idr": _idr(reserve), "cap_idr": _idr(cap)}
    elif side == "sell":
        q = hold.get(coin, Decimal(0))
        if want_pct is not None:
            q = q * min(want_pct, Decimal(100)) / 100
        if want_idr is not None:
            q = min(q, want_idr / px[coin]["bid"])
        q = q.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        price, amount = px[coin]["bid"], q
        order = _idr(q * price)
        limits["holding"] = _idr(pos_val)
        extra = {}
    else:
        raise InputError("side harus buy atau sell")

    binding = min(limits, key=lambda k: limits[k]) if limits else None
    ok, reason = True, None
    if order < cfg["min_order_idr"]:
        ok, reason = False, (f"ukuran Rp {order:,} < minimum order Rp {cfg['min_order_idr']:,}"
                             f" (dibatasi oleh {binding})")
        order, amount = Decimal(0), Decimal(0)
    return {**base, **extra, "price": price, "order_idr": order, "est_amount": amount,
            "est_fee_idr": _idr(order * fee), "limited_by": binding,
            "limits_idr": {k: _idr(v) for k, v in limits.items()}, "ok": ok, "reason": reason,
            "config": {k: v for k, v in cfg.items()},
            "note": "usulan ukuran, bukan order — lanjut cek indodax-risk lalu indodax-broker (paper default)"}


def plan(idr: Decimal, hold: dict, px: dict, cfg: dict, coins) -> dict:
    eq, values = equity_of(idr, hold, px)
    reserve = eq * cfg["cash_buffer_pct"] / 100
    cap = eq * cfg["max_coin_pct"] / 100
    deploy = max(Decimal(0), idr - reserve)
    rows = []
    for c in sorted(set(coins) | set(hold)):
        v = values.get(c, Decimal(0))
        head = max(Decimal(0), cap - v)
        rows.append({"coin": c, "value_idr": _idr(v), "pct": round(v / eq * 100, 2) if eq else Decimal(0),
                     "headroom_idr": _idr(min(head, deploy)), "over_cap": v > cap})
    return {"equity_idr": _idr(eq), "idr_free": _idr(idr), "cash_pct": round(idr / eq * 100, 2) if eq else Decimal(0),
            "reserve_idr": _idr(reserve), "deployable_idr": _idr(deploy), "cap_idr": _idr(cap), "coins": rows,
            "config": cfg, "note": "ringkasan anggaran, bukan order"}


# ------------------------------------------------------------------ CLI

def _json(o):
    return json.dumps(o, ensure_ascii=False, default=lambda x: str(x) if isinstance(x, Decimal) else x)


def _pretty(d: dict) -> str:
    if "coins" in d:
        lines = [f"Equity Rp {d['equity_idr']:,} · kas Rp {d['idr_free']:,} ({d['cash_pct']}%) · "
                 f"cadangan Rp {d['reserve_idr']:,} · bisa dipakai Rp {d['deployable_idr']:,} · cap/koin Rp {d['cap_idr']:,}"]
        for r in d["coins"]:
            lines.append(f"  {r['coin'].upper():6s} Rp {r['value_idr']:>12,} ({r['pct']:>5}%)  ruang beli Rp {r['headroom_idr']:,}"
                         + ("  ⚠ di atas cap" if r["over_cap"] else ""))
        return "\n".join(lines)
    head = f"{d['side'].upper()} {d['pair'].upper()} @ {d['price']:,}"
    if not d["ok"]:
        return f"{head}: TIDAK ADA ORDER — {d['reason']}"
    return (f"{head}: Rp {d['order_idr']:,} ≈ {d['est_amount']} {d['pair'].split('_')[0].upper()} "
            f"(fee ≈ Rp {d['est_fee_idr']:,}) · dibatasi oleh {d['limited_by']}\n"
            + " · ".join(f"{k} Rp {v:,}" for k, v in d["limits_idr"].items()) + f"\n({d['note']})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Position sizing for Indodax (proposal only, no orders)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("size")
    s.add_argument("pair")
    s.add_argument("--side", choices=["buy", "sell"], default="buy")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--want", help="nominal IDR yang diinginkan")
    g.add_argument("--pct", help="persen equity (buy) atau persen holding (sell)")
    s.add_argument("--stop-pct", help="jarak stop-loss dalam %, aktifkan sizing berbasis risiko")
    p = sub.add_parser("plan")
    p.add_argument("--pairs", default="", help="koin tambahan untuk dilihat, mis. btc_idr,eth_idr")
    for x in (s, p):
        x.add_argument("--balance", help="file balance JSON atau '-' untuk stdin")
        x.add_argument("--idr", help="saldo IDR (alternatif --balance)")
        x.add_argument("--hold", help="holding, mis. btc=0.0001,eth=0.002")
        x.add_argument("--price", help="harga manual, mis. btc=1460000000 (tanpa ambil dari market)")
        x.add_argument("--cash-buffer-pct")
        x.add_argument("--max-coin-pct")
        x.add_argument("--risk-pct")
        x.add_argument("--fee-pct")
        x.add_argument("--min-order-idr")
        x.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = dict(DEFAULTS)
        for k in cfg:
            v = getattr(a, k)
            if v is not None:
                cfg[k] = D(v, k)
        if not (0 <= cfg["cash_buffer_pct"] < 100 and 0 < cfg["max_coin_pct"] <= 100 and 0 < cfg["risk_pct"] <= 10
                and 0 <= cfg["fee_pct"] < 5 and cfg["min_order_idr"] >= 0):
            raise InputError("batas konfigurasi di luar rentang wajar")
        if a.balance:
            raw = sys.stdin.read() if a.balance == "-" else Path(a.balance).read_text()
            idr, hold = parse_balance(json.loads(raw))
            hold = {**hold, **parse_kv(a.hold, "--hold")}
        elif a.idr is not None:
            idr, hold = parse_balance({"idr": a.idr, "holdings": {k: str(v) for k, v in parse_kv(a.hold, "--hold").items()}})
        else:
            raise InputError("beri --balance file/'-' atau --idr")
        coins = set(hold)
        if a.cmd == "size":
            coin = coin_of(a.pair)
            coins.add(coin)
        else:
            coins |= {coin_of(x) for x in a.pairs.split(",") if x.strip()}
        manual = parse_kv(a.price, "--price")
        if any(v <= 0 for v in manual.values()):
            raise InputError("--price harus > 0")
        px = manual_prices(manual)
        need = coins - set(px)
        if need:
            px.update(fetch_prices(need))
        if a.cmd == "size":
            want = D(a.want, "--want") if a.want else None
            pct = D(a.pct, "--pct") if a.pct else None
            stop = D(a.stop_pct, "--stop-pct") if a.stop_pct else None
            if (want is not None and want <= 0) or (pct is not None and not 0 < pct <= 100) \
                    or (stop is not None and not 0 < stop < 100):
                raise InputError("--want > 0, --pct 0..100, --stop-pct 0..100")
            d = size(coin, a.side, idr, hold, px, cfg, want, pct, stop)
        else:
            d = plan(idr, hold, px, cfg, coins)
        if d["equity_idr"] <= 0:
            raise InputError("equity 0 — tidak ada yang bisa dianggarkan")
    except MarketDown as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 3
    except (InputError, json.JSONDecodeError, OSError) as e:
        print(_json({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 2
    print(_pretty(d) if a.pretty else _json(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
