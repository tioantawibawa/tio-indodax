#!/usr/bin/env python3
"""indodax-analyst: SMA, EMA and RSI from Indodax candles or trade history (standard library only).

    analyst.py btc_idr [--tf 1D] [--limit 300] [--sma 20,50] [--ema 12,26] [--rsi 14] [--pretty]
    market.py candles btc_idr --tf 60 | analyst.py --input -        # analyse piped market.py output
    market.py trades btc_idr --limit 1000 | analyst.py --input -    # very short-term, from trade prices

Definitions (same as TA-Lib / most charting tools):
- SMA(n): simple mean of the last n closes.
- EMA(n): seeded with SMA of the first n closes, then alpha = 2 / (n + 1).
- RSI(n): Wilder — first averages are simple means of the first n changes, then
  avg = (avg * (n - 1) + x) / n.
Output is information for later pipeline steps, never an order. Exit codes: 0 ok, 2 bad input /
not enough data, 3 market data unavailable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MARKET_DIR = Path(__file__).resolve().parents[2] / "indodax-market" / "scripts"


class InputError(Exception):
    pass


# ------------------------------------------------------------------ indicators (pure)

def sma(values: list[float], n: int) -> list[float | None]:
    if n < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    if n < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    if n < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    ch = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(c, 0.0) for c in ch[:n]) / n
    loss = sum(max(-c, 0.0) for c in ch[:n]) / n

    def val(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[n] = val(gain, loss)
    for i in range(n, len(ch)):
        gain = (gain * (n - 1) + max(ch[i], 0.0)) / n
        loss = (loss * (n - 1) + max(-ch[i], 0.0)) / n
        out[i + 1] = val(gain, loss)
    return out


# ------------------------------------------------------------------ analysis

def series_from(data: dict) -> tuple[list[int], list[float], str, str]:
    """(times, closes, pair, source) from market.py `candles` or `trades` JSON."""
    if "candles" in data:
        rows = data["candles"]
        return ([int(r["time"]) for r in rows], [float(r["close"]) for r in rows],
                data.get("pair", "?"), f"candles {data.get('tf', '?')}")
    if "trades" in data:
        rows = sorted(data["trades"], key=lambda r: (int(r["time"]), str(r.get("tid"))))
        return ([int(r["time"]) for r in rows], [float(r["price"]) for r in rows],
                data.get("pair", "?"), "trades")
    raise InputError("input harus output JSON dari market.py candles atau trades")


def analyse(times, closes, pair, source, sma_p=(20, 50), ema_p=(12, 26), rsi_p=14) -> dict:
    need = max([*sma_p, *ema_p, rsi_p + 1])
    if len(closes) < need:
        raise InputError(f"data {len(closes)} titik, butuh minimal {need} untuk periode yang diminta")
    last = closes[-1]
    ind, sig, reasons = {}, {}, []
    for n in sma_p:
        v = sma(closes, n)[-1]
        ind[f"sma{n}"] = round(v, 6)
        sig[f"price_vs_sma{n}"] = "above" if last > v else "below"
    e = {n: ema(closes, n) for n in ema_p}
    for n, vals in e.items():
        ind[f"ema{n}"] = round(vals[-1], 6)
    r = rsi(closes, rsi_p)
    ind[f"rsi{rsi_p}"] = round(r[-1], 2)
    score = 0
    if len(ema_p) >= 2:
        fast, slow = min(ema_p), max(ema_p)
        now_up = e[fast][-1] > e[slow][-1]
        was_up = e[fast][-2] > e[slow][-2] if e[slow][-2] is not None else now_up
        sig["ema_cross"] = "bullish" if now_up else "bearish"
        sig["fresh_cross"] = now_up != was_up
        score += 1 if now_up else -1
        reasons.append(f"EMA{fast} {'di atas' if now_up else 'di bawah'} EMA{slow}"
                       + (" (baru saja menyilang)" if sig["fresh_cross"] else ""))
        above_slow = last > e[slow][-1]
        score += 1 if above_slow else -1
        reasons.append(f"harga {'di atas' if above_slow else 'di bawah'} EMA{slow}")
    rv = r[-1]
    sig["rsi_zone"] = "overbought" if rv >= 70 else "oversold" if rv <= 30 else "neutral"
    if sig["rsi_zone"] != "neutral":
        reasons.append(f"RSI{rsi_p} {rv:.0f} = {sig['rsi_zone']}")
    bias = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"
    return {"pair": pair, "source": source, "points": len(closes), "last_time": times[-1], "last": last,
            "indicators": ind, "signals": sig, "bias": bias, "reasons": reasons,
            "note": "informasi analisis, bukan perintah order — keputusan lewat indodax-budget/risk/broker"}


def _ints(text: str) -> tuple[int, ...]:
    try:
        vals = tuple(int(x) for x in text.split(",") if x.strip())
    except ValueError as e:
        raise InputError(f"periode harus angka dipisah koma: {text!r}") from e
    if not vals or any(v < 1 or v > 1000 for v in vals):
        raise InputError(f"periode di luar 1..1000: {text!r}")
    return vals


def _pretty(d: dict) -> str:
    ind = "  ".join(f"{k.upper()} {v:,.2f}" for k, v in d["indicators"].items())
    return (f"{d['pair'].upper()} ({d['source']}, {d['points']} titik) · harga {d['last']:,.0f}\n{ind}\n"
            f"Bias: {d['bias'].upper()} — " + "; ".join(d["reasons"]) + f"\n({d['note']})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SMA/EMA/RSI for Indodax pairs (analysis only, no orders)")
    ap.add_argument("pair", nargs="?", help="mis. btc_idr (abaikan bila memakai --input)")
    ap.add_argument("--input", help="file JSON dari market.py candles/trades, atau '-' untuk stdin")
    ap.add_argument("--tf", default="1D")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--sma", default="20,50")
    ap.add_argument("--ema", default="12,26")
    ap.add_argument("--rsi", type=int, default=14)
    ap.add_argument("--pretty", action="store_true")
    a = ap.parse_args(argv)
    try:
        sma_p, ema_p = _ints(a.sma), _ints(a.ema)
        if not 2 <= a.rsi <= 1000:
            raise InputError("--rsi harus 2..1000")
        if a.input:
            raw = sys.stdin.read() if a.input == "-" else Path(a.input).read_text()
            data = json.loads(raw)
        elif a.pair:
            sys.path.insert(0, str(MARKET_DIR))
            import market  # sibling skill
            try:
                data = market.candles(a.pair, a.tf, max(a.limit, max([*sma_p, *ema_p, a.rsi + 1]) + 5))
            except market.InputError as e:
                raise InputError(str(e)) from e
            except market.MarketError as e:
                print(json.dumps({"error": str(e)}), file=sys.stderr)
                return 3
        else:
            raise InputError("beri pair (mis. btc_idr) atau --input")
        d = analyse(*series_from(data), sma_p=sma_p, ema_p=ema_p, rsi_p=a.rsi)
    except (InputError, json.JSONDecodeError, OSError, KeyError, ValueError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 2
    print(_pretty(d) if a.pretty else json.dumps(d, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
