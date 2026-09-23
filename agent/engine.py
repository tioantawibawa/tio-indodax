"""Decision pipeline for one cycle:

    market data -> features/regime -> strategy proposal -> (LLM opinion)
    -> risk manager verdict -> journal every decision in the DB

Execution is not part of this module: approved decisions are returned to the
caller (paper broker in Phase 4, live executor in Phase 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog

from agent.analysis.llm_analyst import LLMAnalyst, apply_opinion
from agent.analysis.signals import MarketSummary, TimeframeFeatures, compute_features
from agent.config import Settings
from agent.data.market_data import PairMarket
from agent.portfolio.portfolio import Portfolio
from agent.risk.risk_manager import AgentStatus, MarketView, RiskContext, RiskDecision, RiskManager
from agent.storage.db import Database
from agent.strategy.strategy import Strategy, TradeProposal

log = structlog.get_logger(__name__)
ZERO = Decimal(0)


class EquityTracker:
    """Persists peak equity and start-of-day equity (Asia/Jakarta day)."""

    def __init__(self, db: Database, mode: str, tz: str = "Asia/Jakarta"):
        self.db, self.mode, self.tz = db, mode, ZoneInfo(tz)

    def local_date(self, now: datetime) -> str:
        return now.astimezone(self.tz).date().isoformat()

    def update(self, equity: Decimal, now: datetime) -> tuple[Decimal, Decimal]:
        """Record ``equity``; return (peak_equity, day_start_equity)."""
        peak = Decimal(self.db.get_state(f"{self.mode}:peak_equity", "0"))
        if equity > peak:
            peak = equity
            self.db.set_state(f"{self.mode}:peak_equity", str(peak))
        date = self.local_date(now)
        row = self.db.get_daily_pnl(date, self.mode)
        if row is None:
            self.db.upsert_daily_pnl(date, self.mode, start_equity=equity, peak_equity=peak)
            day_start = equity
        else:
            day_start = Decimal(row["start_equity"])
        return peak, day_start


@dataclass(frozen=True)
class CycleResult:
    decisions: list[tuple[int, RiskDecision]]      # (decision row id, decision), all verdicts
    notes: dict[str, str]                           # pair -> why no entry was proposed
    trigger_halt: bool
    trigger_daily_stop: bool

    @property
    def approved(self) -> list[tuple[int, RiskDecision]]:
        return [(i, d) for i, d in self.decisions if d.approved]


class DecisionEngine:
    def __init__(self, settings: Settings, db: Database, mode: str,
                 llm: LLMAnalyst | None = None):
        self.s = settings
        self.db = db
        self.mode = mode
        self.llm = llm
        cap = Decimal(str(settings.risk.agent_capital_idr))
        self.strategy = Strategy(settings.strategy, cap)
        self.risk = RiskManager(settings.risk, settings.market, settings.fees)
        self.equity = EquityTracker(db, mode, settings.reporting.timezone)

    def features(self, m: PairMarket) -> dict[str, TimeframeFeatures]:
        return {tf: compute_features(df, tf, self.s.strategy) for tf, df in m.candles.items()}

    def build_context(self, portfolio: Portfolio, marks: Mapping[str, Decimal], now: datetime,
                      status: AgentStatus, reconciliation_ok: bool,
                      pending_buy_idr: Mapping[str, Decimal] | None = None,
                      exchange_free_idr: Decimal | None = None) -> RiskContext:
        equity = portfolio.equity(marks)
        peak, day_start = self.equity.update(equity, now)
        midnight_local = now.astimezone(self.equity.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return RiskContext(
            now=now,
            status=status,
            reconciliation_ok=reconciliation_ok,
            equity_idr=equity,
            peak_equity_idr=peak,
            day_start_equity_idr=day_start,
            cash_idr=portfolio.cash_idr,
            position_qty={k: p.qty for k, p in portfolio.positions.items()},
            position_value_idr={k: p.value(marks[k]) for k, p in portfolio.positions.items()},
            pending_buy_idr=dict(pending_buy_idr or {}),
            orders_last_hour=self.db.count_orders_since(now - timedelta(hours=1), self.mode),
            orders_today=self.db.count_orders_since(midnight_local.astimezone(timezone.utc), self.mode),
            last_stoploss_at=self.db.last_stoploss_times(self.mode),
            exchange_free_idr=exchange_free_idr,
        )

    async def run_cycle(self, markets: Mapping[str, PairMarket], portfolio: Portfolio, now: datetime,
                        status: AgentStatus, reconciliation_ok: bool = True,
                        pending_buy_idr: Mapping[str, Decimal] | None = None,
                        exchange_free_idr: Decimal | None = None) -> CycleResult:
        # Mark positions at best bid (what we could sell for), fall back to last.
        marks = {pair: (m.orderbook.best_bid or m.ticker.last) for pair, m in markets.items()}
        missing = [k for k in portfolio.positions if k not in marks]
        if missing:
            raise ValueError(f"no market data for open positions {missing}")
        ctx = self.build_context(portfolio, marks, now, status, reconciliation_ok,
                                 pending_buy_idr, exchange_free_idr)
        halt, daily, acct_reasons = self.risk.check_account(ctx)
        for r in acct_reasons:
            log.warning("account_limit", reason=r)

        decisions: list[tuple[int, RiskDecision]] = []
        notes: dict[str, str] = {}

        def record(dec: RiskDecision, llm_json: dict | None = None) -> None:
            nonlocal ctx
            row = self.db.record_decision(dec, self.mode, llm_json, ts=now)
            decisions.append((row, dec))
            log.info("decision", pair=dec.proposal.pair, side=dec.proposal.side, verdict=dec.verdict.value,
                     reasons=list(dec.reasons))
            if dec.approved:
                # Later proposals in this cycle must see this order's effect.
                pend = dict(ctx.pending_buy_idr)
                if dec.proposal.side == "buy":
                    pend[dec.proposal.pair] = pend.get(dec.proposal.pair, ZERO) + dec.qty * dec.price
                ctx = replace(
                    ctx, pending_buy_idr=pend,
                    orders_last_hour=ctx.orders_last_hour + (0 if dec.proposal.is_emergency_exit else 1),
                    orders_today=ctx.orders_today + (0 if dec.proposal.is_emergency_exit else 1),
                )

        feats = {pair: self.features(m) for pair, m in markets.items()}

        # 1) exits first — reducing risk takes priority over adding it
        for pair, pos in list(portfolio.positions.items()):
            m = markets[pair]
            prop = self.strategy.propose_exit(pos, m.info, m.orderbook, feats[pair])
            if prop is not None:
                record(self.risk.evaluate(prop, ctx, MarketView(m.info, m.ticker, m.orderbook)))

        # 2) entries
        for pair in self.s.market.whitelist:
            m = markets.get(pair)
            if m is None:
                notes[pair] = "no market data"
                continue
            if pair in portfolio.positions or ctx.pending_buy_idr.get(pair, ZERO) > 0 \
                    or any(d.proposal.pair == pair for _, d in decisions):
                notes[pair] = "already positioned / order pending / exited this cycle"
                continue
            prop, note = self.strategy.propose_entry(pair, m.info, m.orderbook, feats[pair])
            if prop is None:
                notes[pair] = note
                continue
            llm_json = None
            if self.llm is not None:
                summary = self._summary(m, feats[pair])
                opinion = await self.llm.analyze(summary.to_llm_dict(), prop)
                adj = apply_opinion(prop, opinion, self.s.llm, self.s.strategy.min_confidence)
                llm_json = {"opinion": opinion.model_dump() if opinion else None, "note": adj.note}
                prop = replace(prop, confidence=adj.confidence)
                if adj.vetoed:
                    notes[pair] = adj.note
                    record(_llm_veto(prop, adj.note), llm_json)
                    continue
            record(self.risk.evaluate(prop, ctx, MarketView(m.info, m.ticker, m.orderbook)), llm_json)

        return CycleResult(decisions, notes, halt, daily)

    @staticmethod
    def _summary(m: PairMarket, feats: dict[str, TimeframeFeatures]) -> MarketSummary:
        t = m.ticker
        return MarketSummary(
            pair=m.pair, last=float(t.last), bid=float(t.bid), ask=float(t.ask),
            spread_pct=float(m.orderbook.spread_pct or 0), change_24h_pct=None,
            volume_24h_idr=float(t.vol_quote) if t.vol_quote is not None else None, features=feats,
        )


def _llm_veto(p: TradeProposal, note: str) -> RiskDecision:
    from agent.risk.risk_manager import Verdict
    return RiskDecision(Verdict.VETO, p, ZERO, p.price, (note,))
