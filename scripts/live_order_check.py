"""Validate order placement/parsing against the REAL exchange with the smallest possible orders.

    # demo account (AGENT_SETTINGS=config/settings.demo.yaml in .env):
    python -m scripts.live_order_check
    python -m scripts.live_order_check --fill
    # production (real money, hard cap Rp 20.000 per order):
    python -m scripts.live_order_check --production
    python -m scripts.live_order_check --production --fill

Test A: limit buy 10% below the market (does not fill) -> read it back -> cancel -> read again.
Test B (--fill): marketable buy of the minimum size, then sell the same amount back.
Raw API responses (never credentials) are saved to logs/order_check_<time>.json.
NOTE: the Deadman Switch is NOT touched here. Stop the agent service first if you prefer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets as pysecrets
import sys
import time
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from agent.config import load_secrets, load_settings
from agent.exchange.public_client import IndodaxPublicClient
from agent.exchange.signing import Clock
from agent.exchange.trade_client import LiveTradeClient

HARD_CAP_IDR = Decimal(20_000)


def ceil_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="btc_idr")
    ap.add_argument("--fill", action="store_true", help="also run test B (real fill, tiny size)")
    ap.add_argument("--production", action="store_true", help="allow running on the real account")
    args = ap.parse_args(argv)

    sec = load_secrets(".env")
    s = load_settings(sec.AGENT_SETTINGS)
    env = s.exchange.environment
    if env == "production" and not args.production:
        print("Settings point at PRODUCTION. Re-run with --production to use real money (cap Rp 20.000).")
        return 2
    print(f"environment: {env} | tapi: {s.exchange.tapi_url} | pair: {args.pair}")
    log: dict = {"environment": env, "pair": args.pair, "steps": []}

    def rec(step, data):
        log["steps"].append({"step": step, "data": data})
        print(f"\n== {step}\n{json.dumps(data, indent=1, default=str)[:1500]}")

    async with IndodaxPublicClient.from_settings(s.exchange) as pub:
        offset = await pub.clock_offset_ms()
        pairs = await pub.pairs()
        info = pairs[args.pair]
        book = await pub.depth(args.pair)
    tc = LiveTradeClient(sec.INDODAX_API_KEY.get_secret_value(), sec.INDODAX_API_SECRET.get_secret_value(),
                         tapi_url=s.exchange.tapi_url, v2_base_url=s.exchange.tapi_v2_base_url,
                         recv_window_ms=s.exchange.recv_window_ms, clock=Clock(offset_ms=offset))
    ok = True
    try:
        rep = await tc.permission_report()
        rec("permission", {"legacy_ok": rep.legacy_ok, "withdraw_possible": rep.withdraw_possible, "notes": rep.notes})
        if rep.withdraw_possible is not False:
            print("ABORT: key must be proven WITHOUT withdraw permission.")
            return 3
        bid, ask = book.best_bid, book.best_ask
        qty = ceil_step(max(info.min_base or Decimal(0), (info.min_quote or Decimal(0)) * Decimal("1.05") / ask),
                        info.qty_step or Decimal("0.00000001"))
        if qty * ask > HARD_CAP_IDR:
            print(f"ABORT: minimum order {qty * ask:,.0f} IDR exceeds hard cap {HARD_CAP_IDR:,.0f}")
            return 3
        rec("market", {"bid": bid, "ask": ask, "qty": qty, "notional_at_ask": qty * ask, "clock_offset_ms": offset})

        # ---- Test A: resting order, read back, cancel
        price = info.round_price(bid * Decimal("0.90"), "buy")
        coid = f"chk-{pysecrets.token_hex(4)}"
        ack = await tc.place_limit(args.pair, "buy", price, qty, coid)
        rec("A.place (resting)", {"order_id": ack.order_id, "raw": ack.raw})
        st = await tc.get_order_by_coid(coid, args.pair)
        rec("A.read", {"status": st.status if st else None, "orig": st.orig_qty if st else None,
                       "remaining": st.remaining_qty if st else None, "raw": st.raw if st else None})
        ok &= st is not None and st.is_open and st.filled_qty == 0
        opens = await tc.open_orders(args.pair)
        rec("A.openOrders", [o.raw for o in opens if o.client_order_id == coid])
        ok &= any(o.client_order_id == coid for o in opens)
        await tc.cancel(args.pair, ack.order_id, "buy")
        time.sleep(1)
        st = await tc.get_order_by_coid(coid, args.pair)
        rec("A.after cancel", {"status": st.status if st else None, "raw": st.raw if st else None})
        ok &= st is None or st.status == "cancelled"

        if args.fill:
            # ---- Test B: marketable buy then sell back
            coid_b = f"chk-{pysecrets.token_hex(4)}"
            ackb = await tc.place_limit(args.pair, "buy", info.round_price(ask * Decimal("1.002"), "sell"), qty, coid_b)
            rec("B.buy", {"filled_qty": ackb.filled_qty, "filled_quote": ackb.filled_quote, "fee": ackb.fee_idr,
                          "raw": ackb.raw})
            time.sleep(1)
            stb = await tc.get_order_by_coid(coid_b, args.pair)
            rec("B.buy read", {"status": stb.status if stb else None, "filled": stb.filled_qty if stb else None,
                               "raw": stb.raw if stb else None})
            got = stb.filled_qty if stb else ackb.filled_qty
            bal, _ = await tc.balances_legacy()
            base = args.pair.split("_")[0]
            sell_qty = info.round_qty(min(got, bal.free_of(base)))
            rec("B.balance before sell", {base: bal.free_of(base), "idr": bal.free_of("idr"), "sell_qty": sell_qty})
            if sell_qty > 0:
                coid_s = f"chk-{pysecrets.token_hex(4)}"
                acks = await tc.place_limit(args.pair, "sell", info.round_price(bid * Decimal("0.998"), "buy"),
                                            sell_qty, coid_s)
                rec("B.sell", {"filled_qty": acks.filled_qty, "filled_quote": acks.filled_quote,
                               "fee": acks.fee_idr, "raw": acks.raw})
                time.sleep(1)
                sts = await tc.get_order_by_coid(coid_s, args.pair)
                rec("B.sell read", {"status": sts.status if sts else None, "raw": sts.raw if sts else None})
                ok &= sts is not None and sts.status == "filled"
            ok &= stb is not None and stb.status == "filled"
    except Exception as e:  # noqa: BLE001
        rec("ERROR", f"{type(e).__name__}: {e}")
        ok = False
    finally:
        await tc.aclose()
        Path("logs").mkdir(exist_ok=True)
        out = Path("logs") / f"order_check_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(log, indent=1, default=str))
        print(f"\nraw responses saved to {out}")
    print("\nORDER CHECK", "PASSED" if ok else "FAILED — send the output to the developer before going live")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
