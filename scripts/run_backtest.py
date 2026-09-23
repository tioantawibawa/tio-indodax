"""Run a backtest on downloaded history and write a report.

    python -m scripts.download_history --months 6
    python -m scripts.run_backtest --data data/history --out reports/backtest

Uses config/settings.yaml (strategy, risk limits, fees) exactly as live.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from agent.backtest.backtester import BacktestConfig, Backtester, load_history, load_pair_infos
from agent.backtest.report import write_report
from agent.config import load_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/history")
    ap.add_argument("--out", default="reports/backtest")
    ap.add_argument("--settings", default="config/settings.yaml")
    ap.add_argument("--months", type=float, default=None, help="only the last N months")
    ap.add_argument("--start", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", help="YYYY-MM-DD (UTC), exclusive")
    ap.add_argument("--spread", type=float, default=0.10)
    ap.add_argument("--slippage", type=float, default=0.10)
    args = ap.parse_args(argv)

    s = load_settings(args.settings)
    infos = load_pair_infos(args.data)
    data = {p: {tf: load_history(args.data, p, tf) for tf in s.strategy.timeframes} for p in s.market.whitelist}
    data = {p: d for p, d in data.items() if all(len(v) for v in d.values())}
    start = None
    if args.months:
        start = datetime.now(timezone.utc) - timedelta(days=30 * args.months)
    if args.start:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else None
    r = Backtester(s, data, infos, BacktestConfig(spread_pct=args.spread, slippage_pct=args.slippage),
                   start=start, end=end).run()
    path = write_report(r, args.out, title=f"Backtest {', '.join(s.market.whitelist)}")
    print(path.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
