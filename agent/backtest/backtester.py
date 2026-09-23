"""Event-driven backtester that runs the REAL decision pipeline.

The same ``Strategy``, ``RiskManager``, ``Portfolio`` and ``DecisionEngine``
used live make every decision here; only market data and fills are simulated.

Simulation model (conservative where a choice exists):
- Timeline: every close of an entry-timeframe candle (15m). Higher
  timeframes only contribute candles that had already closed — no lookahead.
- Quotes: historical orderbooks are not available, so bid/ask are the close
  -/+ half of an assumed spread; depth per level is a fraction of the bar's
  volume; 24h IDR volume = rolling sum of volume x close (assumes candle
  ``Volume`` is in the base asset — see docs/indodax_api_notes.md).
- Entry: a marketable limit buy (limit >= best ask) fills immediately at the
  ask with the taker fee — as it would live. A passive limit (below the ask)
  is valid for the next candle only and fills only if price trades *through*
  it (low < limit), with the maker fee.
- Pairs join the timeline when their data starts (e.g. SOL from 2021-11).
- Stop-loss: checked intrabar; fill at min(open, SL) minus slippage, taker
  fee. If SL and TP are both touched in one candle, SL is assumed first.
- Take-profit: intrabar, fill at TP, taker fee.
- Strategy exits (regime change) at candle close: fill at bid, taker fee.
- Daily-loss stop -> PAUSED until the next Jakarta day; drawdown kill
  switch -> HALTED for the rest of the run (no manual /resume in a backtest).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import structlog

from agent.analysis.costs import CostModel
from agent.analysis.regime import min_bars
from agent.analysis.signals import TimeframeFeatures, feature_frame, row_to_features
from agent.config import Settings
from agent.data.market_data import PairMarket
from agent.engine import DecisionEngine
from agent.exchange.models import TIMEFRAMES, OrderBook, PairInfo, Ticker
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import AgentStatus, Verdict
from agent.storage.db import Database

log = structlog.get_logger(__name__)
ZERO = Decimal(0)
MODE = "backtest"


@dataclass(frozen=True)
class BacktestConfig:
    spread_pct: float = 0.10       # assumed bid-ask spread
    slippage_pct: float = 0.10     # extra adverse price on stop-loss / market exits
    depth_fraction: float = 0.2    # orderbook depth per level, as a fraction of the bar's volume
    level_step_pct: float = 0.05   # distance between synthetic orderbook levels


@dataclass
class Trade:
    pair: str
    setup: str
    entry_time: datetime
    entry_price: Decimal
    qty: Decimal
    entry_fee: Decimal
    exit_time: datetime | None = None
    exit_price: Decimal | None = None
    exit_fee: Decimal = ZERO
    exit_reason: str = ""
    pnl: Decimal | None = None      # net of both fees

    @property
    def fees(self) -> Decimal:
        return self.entry_fee + self.exit_fee


@dataclass
class BacktestResult:
    start: datetime
    end: datetime
    initial_equity: Decimal
    equity: pd.Series                       # equity marked at bid, per step
    trades: list[Trade]
    verdicts: Counter
    veto_reasons: Counter
    events: list[tuple[datetime, str]]
    fees_paid: Decimal
    spread_slippage_cost: Decimal
    buy_and_hold_pct: dict[str, float]
    config: BacktestConfig
    metrics: dict = field(default_factory=dict)


# ----------------------------------------------------------------- data I/O

def load_history(data_dir: str | Path, pair: str, tf: str) -> pd.DataFrame:
    df = pd.read_csv(Path(data_dir) / f"{pair}_{tf}.csv")
    df.index = pd.to_datetime(df.pop("ts"), unit="s", utc=True)
    df.index.name = "ts"
    df = df.astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def load_pair_infos(data_dir: str | Path) -> dict[str, PairInfo]:
    raw = json.loads((Path(data_dir) / "pairs.json").read_text())
    ticks = {k: Decimal(str(v)) for k, v in raw["price_increments"]["increments"].items()}
    return {p["ticker_id"]: PairInfo.from_api(p, price_tick=ticks.get(p["ticker_id"]))
            for p in raw["pairs"] if p.get("ticker_id")}


# ----------------------------------------------------------------- engine

class _BacktestEngine(DecisionEngine):
    """DecisionEngine whose features come from precomputed per-bar frames."""

    current: dict[str, dict[str, TimeframeFeatures]]

    def features(self, m: PairMarket) -> dict[str, TimeframeFeatures]:
        return self.current[m.pair]


def _d(x: float) -> Decimal:
    return Decimal(repr(float(x)))


class Backtester:
    def __init__(self, settings: Settings, data: dict[str, dict[str, pd.DataFrame]],
                 pair_infos: dict[str, PairInfo], cfg: BacktestConfig = BacktestConfig(),
                 start: datetime | None = None, end: datetime | None = None, db_path: str = ":memory:"):
        self.s = settings
        self.cfg = cfg
        self.pairs = [p for p in settings.market.whitelist if p in data]
        missing = [p for p in self.pairs if p not in pair_infos]
        if missing:
            raise ValueError(f"no pair info for {missing}")
        if not self.pairs:
            raise ValueError("no data for any whitelisted pair")
        self.infos = pair_infos
        self.tfs = settings.strategy.timeframes
        self.entry_tf = self.tfs[0]
        self.data = data
        self.db = Database(db_path)
        self.engine = _BacktestEngine(settings, self.db, MODE)
        self.costs = CostModel(settings.fees)
        self.tz = ZoneInfo(settings.reporting.timezone)
        self.start, self.end = start, end

        # Never use a candle that had not closed yet when the data was downloaded.
        now_s = int(datetime.now(timezone.utc).timestamp())
        data = {p: {tf: df[df.index.as_unit("s").asi8 + TIMEFRAMES[tf] <= now_s] for tf, df in d.items()}
                for p, d in data.items()}
        self.data = data

        # Precompute features and close times per (pair, tf).
        self.frames: dict[tuple[str, str], pd.DataFrame] = {}
        self.close_times: dict[tuple[str, str], np.ndarray] = {}
        self.vol24: dict[str, pd.Series] = {}
        for p in self.pairs:
            for tf in self.tfs:
                df = data[p][tf]
                self.frames[(p, tf)] = feature_frame(df, settings.strategy)
                self.close_times[(p, tf)] = df.index.as_unit("s").asi8 + TIMEFRAMES[tf]
            base = data[p][self.entry_tf]
            self.vol24[p] = (base["volume"] * base["close"]).rolling(
                int(86400 / TIMEFRAMES[self.entry_tf]), min_periods=1).sum()

    # ------------------------------------------------------------ helpers

    def _features_at(self, pair: str, t_close: int) -> dict[str, TimeframeFeatures] | None:
        out = {}
        for tf in self.tfs:
            ct = self.close_times[(pair, tf)]
            i = int(np.searchsorted(ct, t_close, side="right")) - 1
            if i < 0:
                return None
            out[tf] = row_to_features(self.frames[(pair, tf)].iloc[i], tf, self.s.strategy)
        return out

    def _quote(self, pair: str, bar: pd.Series, vol24: float, ts: int) -> tuple[Ticker, OrderBook]:
        info = self.infos[pair]
        c = _d(bar["close"])
        half = _d(self.cfg.spread_pct) / 200
        bid = info.round_price(c * (1 - half), "buy")
        ask = info.round_price(c * (1 + half), "sell")
        if ask <= bid:
            ask = bid + (info.price_tick or Decimal("0.00000001"))
        depth = max(_d(bar["volume"]) * _d(self.cfg.depth_fraction), Decimal("0.00000001"))
        step = _d(self.cfg.level_step_pct) / 100
        bids = tuple((info.round_price(bid * (1 - k * step), "buy"), depth) for k in range(5))
        asks = tuple((info.round_price(ask * (1 + k * step), "sell"), depth) for k in range(5))
        t = Ticker(pair=pair, last=c, bid=bid, ask=ask, high=_d(bar["high"]), low=_d(bar["low"]),
                   vol_base=None, vol_quote=_d(vol24), server_time=ts)
        return t, OrderBook(pair=pair, bids=bids, asks=asks)

    def _fee(self, pair: str, notional: Decimal, maker: bool) -> Decimal:
        return self.costs.fee_idr(self.infos[pair], notional, maker)

    # ------------------------------------------------------------ run

    def run(self) -> BacktestResult:
        return asyncio.run(self._run())

    async def _run(self) -> BacktestResult:
        cap = Decimal(str(self.s.risk.agent_capital_idr))
        pf = Portfolio(cap)
        base_idx = None
        for p in self.pairs:
            idx = self.data[p][self.entry_tf].index
            base_idx = idx if base_idx is None else base_idx.union(idx)
        dur = TIMEFRAMES[self.entry_tf]
        # warm-up: start once the first pair has enough history on its slowest TF
        st = self.s.strategy
        need_bars = max(min_bars(st.ema_slow, st.adx_period, st.atr_period),
                        st.trend_ema, st.breakout_bars, st.chandelier_bars) + 10
        warm_close = None
        for p in self.pairs:
            bars_col = self.frames[(p, self.tfs[-1])]["bars"]
            ok = bars_col[bars_col >= need_bars]
            if ok.empty:
                continue
            c = int(ok.index[0].timestamp()) + TIMEFRAMES[self.tfs[-1]]
            warm_close = c if warm_close is None else min(warm_close, c)
        if warm_close is None:
            raise ValueError(f"not enough {self.tfs[-1]} candles for warm-up ({need_bars} needed)")
        steps = [ts for ts in base_idx if int(ts.timestamp()) + dur >= warm_close]
        if self.start is not None:
            steps = [ts for ts in steps if ts >= self.start]
        if self.end is not None:
            steps = [ts for ts in steps if ts < self.end]
        if not steps:
            raise ValueError("no bars to simulate after warm-up / date filter")

        status = AgentStatus.RUNNING
        paused_day: str | None = None
        pending: list[dict] = []        # buy orders placed at the previous step
        open_trades: dict[str, Trade] = {}
        trades: list[Trade] = []
        verdicts: Counter = Counter()
        veto_reasons: Counter = Counter()
        events: list[tuple[datetime, str]] = []
        equity_pts: list[tuple[datetime, float]] = []
        spread_slip = ZERO
        seq = 0

        def new_order(pair, side, otype, price, qty, t, decision_id, emergency, status) -> str:
            nonlocal seq
            seq += 1
            coid = f"bt-{seq}"
            self.db.record_order(client_order_id=coid, decision_id=decision_id, mode=MODE, pair=pair,
                                 side=side, order_type=otype, price=price, qty=qty, status=status,
                                 is_emergency=emergency, ts=t)
            return coid

        def fill(pair: str, side: str, qty: Decimal, price: Decimal, fee: Decimal, t: datetime,
                 reason: str, emergency: bool = False, sl=None, tp=None, setup: str = "",
                 decision_id: int | None = None, coid: str | None = None) -> None:
            if coid is None:
                coid = new_order(pair, side, "market" if emergency else "limit", price, qty, t,
                                 decision_id, emergency, "FILLED")
            else:
                self.db.update_order(coid, status="FILLED", filled_qty=qty, ts=t)
            r = pf.apply_fill(pair, side, qty, price, fee, t, sl, tp, reason)
            self.db.record_fill(trade_id=coid, client_order_id=coid, ts=t, mode=MODE, pair=pair, side=side,
                                price=price, qty=qty, fee_idr=fee, realized_pnl=r.realized_pnl)
            if side == "buy":
                open_trades[pair] = Trade(pair, setup, t, price, qty, fee)
            else:
                tr = open_trades.pop(pair)
                tr.exit_time, tr.exit_price, tr.exit_fee, tr.exit_reason = t, price, fee, reason
                tr.pnl = (price - tr.entry_price) * qty - tr.entry_fee - fee
                trades.append(tr)

        for ts in steps:
            t_open = int(ts.timestamp())
            t_close = t_open + dur
            now = datetime.fromtimestamp(t_close, tz=timezone.utc)
            bars = {p: self.data[p][self.entry_tf].loc[ts] for p in self.pairs
                    if ts in self.data[p][self.entry_tf].index}

            # 1) resolve buy orders placed at the previous close against this candle
            for o in pending:
                bar = bars[o["pair"]]
                if _d(bar["low"]) < o["price"]:
                    notional = o["qty"] * o["price"]
                    fill(o["pair"], "buy", o["qty"], o["price"], self._fee(o["pair"], notional, True),
                         now, o["reason"], sl=o["sl"], tp=o["tp"], setup=o["setup"], coid=o["coid"])
                else:
                    self.db.update_order(o["coid"], status="CANCELLED", ts=now)
            pending = []

            # 2) intrabar stop-loss / take-profit
            for pair, pos in list(pf.positions.items()):
                if pair not in bars:
                    continue
                bar = bars[pair]
                lo, hi, op = _d(bar["low"]), _d(bar["high"]), _d(bar["open"])
                if pos.stop_loss is not None and lo <= pos.stop_loss:
                    ref = min(op, pos.stop_loss)
                    px = self.infos[pair].round_price(ref * (1 - _d(self.cfg.slippage_pct) / 100), "buy")
                    spread_slip += (pos.stop_loss - px) * pos.qty
                    fill(pair, "sell", pos.qty, px, self._fee(pair, pos.qty * px, False), now,
                         "stop_loss", emergency=True)
                    self.db.record_stoploss(pair, MODE, now)
                elif pos.take_profit is not None and hi >= pos.take_profit:
                    px = pos.take_profit
                    fill(pair, "sell", pos.qty, px, self._fee(pair, pos.qty * px, False), now, "take_profit")

            # 3) status transitions (daily stop lasts until the next Jakarta day)
            day = now.astimezone(self.tz).date().isoformat()
            if status == AgentStatus.PAUSED and paused_day is not None and day != paused_day:
                status, paused_day = AgentStatus.RUNNING, None
                events.append((now, "RESUMED (new day)"))

            # 4) run the real decision pipeline at candle close
            markets = {}
            feats = {}
            for p in bars:
                f = self._features_at(p, t_close)
                if f is None:
                    continue
                tk, ob = self._quote(p, bars[p], float(self.vol24[p].loc[ts]), t_close)
                markets[p] = PairMarket(pair=p, info=self.infos[p], ticker=tk, orderbook=ob,
                                        candles={}, fetched_at=float(t_close))
                feats[p] = f
            if set(pf.positions) - set(markets):
                continue  # cannot value positions without data; skip step
            self.engine.current = feats
            res = await self.engine.run_cycle(markets, pf, now, status)
            for row_id, d in res.decisions:
                verdicts[d.verdict.value] += 1
                if d.verdict == Verdict.VETO:
                    veto_reasons[_normalise_reason(d.reasons[0])] += 1
                if not d.approved:
                    continue
                p = d.proposal
                m = markets[p.pair]
                if p.side == "buy" and d.price >= m.orderbook.best_ask:
                    # marketable limit: fills now at the ask (taker)
                    px = m.orderbook.best_ask
                    spread_slip += (px - m.ticker.last) * d.qty
                    fill(p.pair, "buy", d.qty, px, self._fee(p.pair, d.qty * px, False), now, p.reason,
                         sl=p.stop_loss, tp=p.take_profit, setup=p.setup, decision_id=row_id)
                elif p.side == "buy":
                    coid = new_order(p.pair, "buy", "limit", d.price, d.qty, now, row_id, False, "NEW")
                    pending.append(dict(pair=p.pair, price=d.price, qty=d.qty, sl=p.stop_loss, tp=p.take_profit,
                                        reason=p.reason, setup=p.setup, coid=coid))
                else:
                    px = d.price
                    if p.order_type == "market":
                        px = self.infos[p.pair].round_price(
                            m.orderbook.best_bid * (1 - _d(self.cfg.slippage_pct) / 100), "buy")
                    spread_slip += (m.ticker.last - px) * d.qty
                    fill(p.pair, "sell", d.qty, px, self._fee(p.pair, d.qty * px, False), now,
                         p.setup or "exit", emergency=p.is_emergency_exit, decision_id=row_id)
                    if p.setup == "stop_loss":
                        self.db.record_stoploss(p.pair, MODE, now)
            if res.trigger_halt and status != AgentStatus.HALTED:
                status = AgentStatus.HALTED
                for o in pending:
                    self.db.update_order(o["coid"], status="CANCELLED", ts=now)
                pending = []
                events.append((now, "HALTED: max drawdown kill switch"))
            elif res.trigger_daily_stop and status == AgentStatus.RUNNING:
                status, paused_day = AgentStatus.PAUSED, day
                events.append((now, "PAUSED: daily loss limit"))

            marks = {p: markets[p].orderbook.best_bid for p in pf.positions}
            equity_pts.append((now, float(pf.equity(marks))))

        # record open positions as open trades (marked, not closed)
        for tr in open_trades.values():
            tr.exit_reason = "open at end"
            trades.append(tr)
        equity = pd.Series([e for _, e in equity_pts], index=pd.DatetimeIndex([t for t, _ in equity_pts]))
        first, last = steps[0], steps[-1]
        bh = {}
        for p in self.pairs:
            c = self.data[p][self.entry_tf]["close"]
            c = c[(c.index >= first) & (c.index <= last)]
            if len(c) > 1:
                bh[p] = float((c.iloc[-1] / c.iloc[0] - 1) * 100)
        result = BacktestResult(
            start=datetime.fromtimestamp(int(first.timestamp()) + dur, tz=timezone.utc),
            end=datetime.fromtimestamp(int(last.timestamp()) + dur, tz=timezone.utc),
            initial_equity=cap, equity=equity, trades=trades, verdicts=verdicts, veto_reasons=veto_reasons,
            events=events, fees_paid=pf.fees_paid, spread_slippage_cost=spread_slip,
            buy_and_hold_pct=bh, config=self.cfg,
        )
        result.metrics = compute_metrics(result)
        return result


def _normalise_reason(r: str) -> str:
    """Group veto reasons by type: strip numbers and pair names."""
    r = re.sub(r"[a-z0-9]+_(idr|usdt|btc)", "<pair>", r)
    r = re.sub(r"-?\d[\d.,:+\-T]*%?", "#", r)
    return r[:90]


# ----------------------------------------------------------------- metrics

def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((peak - equity) / peak).max() * 100)


def sharpe_daily(equity: pd.Series) -> float:
    daily = equity.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) < 2 or rets.std(ddof=1) == 0:
        return 0.0
    return float(rets.mean() / rets.std(ddof=1) * np.sqrt(365))


def compute_metrics(r: BacktestResult) -> dict:
    closed = [t for t in r.trades if t.pnl is not None]
    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl <= 0]
    end_eq = Decimal(str(r.equity.iloc[-1])) if not r.equity.empty else r.initial_equity
    net = end_eq - r.initial_equity
    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    days = max((r.end - r.start).total_seconds() / 86400, 1e-9)
    ret_pct = float(net / r.initial_equity * 100)
    return {
        "period_days": round(days, 1),
        "initial_equity_idr": float(r.initial_equity),
        "final_equity_idr": float(end_eq),
        "net_pnl_idr": float(net),
        "total_return_pct": round(ret_pct, 3),
        "annualized_return_pct": round(((1 + ret_pct / 100) ** (365 / days) - 1) * 100, 2) if ret_pct > -100 else -100.0,
        "max_drawdown_pct": round(max_drawdown_pct(r.equity), 3),
        "sharpe_daily_annualized": round(sharpe_daily(r.equity), 3),
        "trades_closed": len(closed),
        "trades_open_at_end": len(r.trades) - len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win_idr": float(gross_win / len(wins)) if wins else None,
        "avg_loss_idr": float(-gross_loss / len(losses)) if losses else None,
        "profit_factor": round(float(gross_win / gross_loss), 3) if gross_loss > 0 else None,
        "total_fees_idr": float(r.fees_paid),
        "pnl_before_fees_idr": float(net + r.fees_paid),
        "spread_slippage_cost_idr": float(r.spread_slippage_cost),
        "exit_reasons": dict(Counter(t.exit_reason for t in r.trades)),
        "setups": dict(Counter(t.setup for t in r.trades)),
        "buy_and_hold_pct": {k: round(v, 2) for k, v in r.buy_and_hold_pct.items()},
        "verdicts": dict(r.verdicts),
        "top_veto_reasons": r.veto_reasons.most_common(8),
        "events": [(t.isoformat(), e) for t, e in r.events],
    }
