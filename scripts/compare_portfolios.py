"""Reproducible chronological comparison; never edits active settings or trades.

All variants share the corrected cost/execution engine. The baseline is the
supplied settings, not a replay of an older code revision.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import structlog

from agent.backtest.backtester import BacktestConfig, Backtester, load_history, load_pair_infos
from agent.config import Settings, load_settings


VARIANTS = {
    "baseline": {},
    "buffer_1pct": {"trend_buffer_pct": 1.0},
    "minimum_aware": {"minimum_aware": True},
    "slots_3": {"max_positions": 3, "minimum_aware": True},
    "combined": {"trend_buffer_pct": 1.0, "max_positions": 3, "minimum_aware": True},
    "maker_first": {"trend_buffer_pct": 1.0, "max_positions": 3, "minimum_aware": True,
                    "execution_style": "maker_first"},
}


def variant_settings(s: Settings, variant: str, cost_multiplier: float) -> Settings:
    raw = s.model_dump()
    raw["portfolio"].update(VARIANTS[variant])
    for key in ("maker_pct", "taker_pct", "tax_pct", "clearing_pct"):
        raw["fees"][key] *= cost_multiplier
    return Settings.model_validate(raw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data/history")
    ap.add_argument("--settings", default="config/settings.yaml")
    ap.add_argument("--out", default="reports/optimization/summary.json")
    ap.add_argument("--start", required=True)
    ap.add_argument("--split", required=True, help="chronological verification start; choose before running")
    ap.add_argument("--end", required=True, help="exclusive")
    ap.add_argument("--variants", nargs="+", choices=list(VARIANTS))
    ap.add_argument("--execution-timeframe")
    ap.add_argument("--signal-delay", type=int, default=0)
    ap.add_argument("--fill-fraction", type=float, default=1.0)
    args = ap.parse_args(argv)
    args.variants = args.variants or [v for v in VARIANTS if args.execution_timeframe or v != "maker_first"]
    dates = [datetime.fromisoformat(x).replace(tzinfo=timezone.utc) for x in (args.start, args.split, args.end)]
    if not dates[0] < dates[1] < dates[2]:
        ap.error("require start < split < end")
    if "baseline" not in args.variants:
        ap.error("baseline is required for comparison")
    s = load_settings(args.settings)
    if not s.portfolio.enabled:
        ap.error("comparison requires portfolio.enabled=true")
    if "maker_first" in args.variants and not args.execution_timeframe:
        ap.error("maker_first requires intraday --execution-timeframe; daily fills are too coarse")
    tfs = set(s.strategy.timeframes) | ({args.execution_timeframe} if args.execution_timeframe else set())
    paths = [Path(args.data) / "pairs.json"] + [Path(args.data) / f"{pair}_{tf}.csv"
             for pair in s.market.whitelist for tf in sorted(tfs)]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    data = {p: {tf: load_history(args.data, p, tf) for tf in tfs} for p in s.market.whitelist}
    infos = load_pair_infos(args.data)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
    rows = []
    for window, start, end in (("development", dates[0], dates[1]), ("verification", dates[1], dates[2])):
        for multiplier in (1, 2):
            # Stress includes API per-pair fees, fallback fees, taxes, spread and slippage.
            stressed_infos = {k: replace(v,
                maker_fee_pct=v.maker_fee_pct * multiplier if v.maker_fee_pct is not None else None,
                taker_fee_pct=v.taker_fee_pct * multiplier if v.taker_fee_pct is not None else None)
                for k, v in infos.items()}
            for variant in args.variants:
                cfg = BacktestConfig(spread_pct=0.1 * multiplier, slippage_pct=0.1 * multiplier,
                    execution_timeframe=args.execution_timeframe, signal_delay_s=args.signal_delay,
                    fill_fraction=args.fill_fraction)
                selected = variant_settings(s, variant, multiplier)
                bt = Backtester(selected, data, stressed_infos, cfg, start=start, end=end)
                try:
                    r = bt.run()
                    rows.append({"window": window, "cost_multiplier": multiplier, "variant": variant,
                                 "start": r.start.isoformat(), "end": r.end.isoformat(),
                                 "metrics": r.metrics, "portfolio_settings": selected.portfolio.model_dump()})
                    print(f"{window} cost={multiplier}x {variant}: return={r.metrics['total_return_pct']}% "
                          f"DD={r.metrics['max_drawdown_pct']}% closed={r.metrics['trades_closed']}", flush=True)
                finally:
                    bt.db.close()
    decisions = {}
    for variant in args.variants:
        if variant == "baseline":
            continue
        reasons = []
        for row in (x for x in rows if x["variant"] == variant):
            base = next(x for x in rows if x["variant"] == "baseline" and
                        x["window"] == row["window"] and x["cost_multiplier"] == row["cost_multiplier"])
            m = row["metrics"]
            tag = f"{row['window']}/{row['cost_multiplier']}x"
            if m["trades_closed"] < 10:
                reasons.append(f"{tag}: fewer than 10 closed trades")
            if m["total_return_pct"] <= max(0, base["metrics"]["total_return_pct"]):
                reasons.append(f"{tag}: net return not positive and better than baseline")
            if m["max_drawdown_pct"] > min(30, base["metrics"]["max_drawdown_pct"] + 2):
                reasons.append(f"{tag}: drawdown above baseline+2pp or 30%")
        decisions[variant] = {"passes_screen": not reasons, "reasons": reasons}
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "source_sha256": hashes,
               "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted([*Path("agent").rglob("*.py"), Path(__file__)])},
               "settings_sha256": hashlib.sha256(Path(args.settings).read_bytes()).hexdigest(),
               "requested_period": [args.start, args.split, args.end], "rows": rows, "screen": decisions,
               "limitations": ["Verification dates may overlap earlier project research; not a pristine holdout.",
                   "OHLC synthetic books cannot reproduce historical queue position, outages or real partial fills.",
                   "Passive fills last one execution bar; intrabar protective stops assume full execution.",
                   "No automatic parameter promotion; screen is not proof of future profitability."]}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
