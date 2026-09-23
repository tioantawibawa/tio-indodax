"""Download historical OHLC candles + pair info for backtesting (read-only, public API).

    python -m scripts.download_history --months 6

Writes data/history/<pair>_<tf>.csv (ts,open,high,low,close,volume; ts = candle
open time, epoch seconds UTC) and data/history/pairs.json. Fetches one extra
month for indicator warm-up. Re-running only fetches candles newer than the
last saved one.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from pathlib import Path

from agent.config import load_settings
from agent.data.market_data import closed_only
from agent.exchange.public_client import IndodaxPublicClient

WARMUP_DAYS = 30


def last_ts(path: Path) -> int | None:
    if not path.exists():
        return None
    last = None
    with path.open() as f:
        for row in csv.DictReader(f):
            last = int(row["ts"])
    return last


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=6)
    ap.add_argument("--out", default="data/history")
    ap.add_argument("--pairs", nargs="*", help="default: whitelist from settings.yaml")
    ap.add_argument("--timeframes", nargs="*", help="default: strategy timeframes")
    ap.add_argument("--since", help="start date YYYY-MM-DD (overrides --months)")
    args = ap.parse_args(argv)

    s = load_settings()
    pairs = args.pairs or list(s.market.whitelist)
    tfs = args.timeframes or list(s.strategy.timeframes)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    start = now - int((args.months * 30 + WARMUP_DAYS) * 86400)
    if args.since:
        from datetime import datetime, timezone
        start = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp())

    async with IndodaxPublicClient.from_settings(s.exchange) as c:
        raw = await c._get("/api/pairs")
        incs = await c._get("/api/price_increments")
        (out / "pairs.json").write_text(json.dumps({"pairs": raw, "price_increments": incs}, indent=1))
        for pair in pairs:
            for tf in tfs:
                path = out / f"{pair}_{tf}.csv"
                prev = last_ts(path)
                frm = prev + 1 if prev else start
                candles = await c.ohlc(pair, tf, frm, now)
                candles = closed_only(candles, tf, now)  # never store a still-forming candle
                new = path.exists()
                with path.open("a", newline="") as f:
                    w = csv.writer(f)
                    if not new:
                        w.writerow(["ts", "open", "high", "low", "close", "volume"])
                    for k in candles:
                        w.writerow([k.ts, k.open, k.high, k.low, k.close, k.volume])
                first = candles[0].ts if candles else None
                print(f"{pair} tf={tf}: +{len(candles)} candles (first new ts={first}) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
