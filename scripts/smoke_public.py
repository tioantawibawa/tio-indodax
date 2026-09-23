"""Read-only smoke test against the REAL Indodax public API.

Run on the VPS (no API key needed, sends no orders):
    python -m scripts.smoke_public

Checks every public endpoint the agent uses, the pair formats, and the VPS
clock offset vs the Indodax server.
"""

from __future__ import annotations

import asyncio
import sys
import time
from decimal import Decimal

from agent.config import load_settings
from agent.exchange.public_client import IndodaxPublicClient


async def main() -> int:
    s = load_settings()
    ok = True
    async with IndodaxPublicClient.from_settings(s.exchange) as c:
        offset = await c.clock_offset_ms()
        flag = "OK" if abs(offset) <= s.exchange.max_clock_offset_ms else "TOO LARGE — fix NTP/chrony!"
        print(f"clock offset (server - local): {offset} ms  [{flag}]")
        ok &= abs(offset) <= s.exchange.max_clock_offset_ms

        pairs = await c.pairs()
        print(f"pairs: {len(pairs)} listed")
        for p in s.market.whitelist:
            info = pairs.get(p)
            if info is None:
                print(f"  {p}: NOT FOUND on exchange"); ok = False; continue
            print(f"  {p}: tick={info.price_tick} qty_step={info.qty_step} min_idr={info.min_quote} "
                  f"min_coin={info.min_base} maker={info.maker_fee_pct}% taker={info.taker_fee_pct}% "
                  f"tradable={info.tradable}")

        for p in s.market.whitelist:
            t = await c.ticker(p)
            ob = await c.depth(p)
            tr = await c.trades(p)
            now = int(time.time())
            candles = await c.ohlc(p, "60", now - 48 * 3600, now)
            est = ob.estimate_fill("buy", Decimal(1_000_000))
            print(f"  {p}: last={t.last} bid={t.bid} ask={t.ask} spread={t.spread_pct:.3f}% "
                  f"vol_idr={t.vol_quote} | depth {len(ob.bids)}b/{len(ob.asks)}a | trades={len(tr)} "
                  f"| 1h candles(48h)={len(candles)} | slippage Rp1jt buy={est.slippage_pct}")
            ok &= len(candles) > 0
    print("SMOKE TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
