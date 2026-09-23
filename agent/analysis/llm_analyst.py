"""Optional LLM analyst (Claude) — a second opinion, never a decision maker.

Boundaries enforced in code:
- Input: the structured ``MarketSummary.to_llm_dict()`` plus the proposal's
  side/setup/levels. No balances, no keys, no raw candles.
- Output: JSON ``{bias, confidence, reasoning, risks}`` validated by pydantic.
- Effect: :func:`apply_opinion` may move the proposal's confidence by at most
  ``max_confidence_adjust`` and may suppress a BUY when the LLM is bearish
  with confidence >= ``veto_confidence``. It cannot create orders, change
  price/qty/SL/TP, or touch risk limits (the risk manager never sees it).
- Any timeout, API error, refusal or malformed output -> ``None`` and the
  cycle continues without the LLM.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from agent.config import LLMSettings
from agent.strategy.strategy import TradeProposal

log = structlog.get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You are a cautious crypto market analyst reviewing a proposed spot trade on the \
Indodax exchange (IDR pairs). You receive pre-computed indicators for several timeframes and the \
proposed trade. Judge whether current conditions support the trade's direction over the next few \
hours to a day.

Respond only with the JSON object required by the schema:
- bias: "bullish", "bearish" or "neutral" for this pair over that horizon.
- confidence: 0 to 1, how sure you are of the bias. Use values above 0.7 only when several \
timeframes clearly agree.
- reasoning: two to four sentences citing the specific indicator values you relied on.
- risks: up to four short phrases naming what would invalidate the trade.

You cannot place, size or modify orders; your output only nudges the trade's confidence score."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["bias", "confidence", "reasoning", "risks"],
    "additionalProperties": False,
}


class LLMOpinion(BaseModel):
    bias: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(max_length=2000)
    risks: list[str] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class AdjustedConfidence:
    confidence: float
    vetoed: bool
    note: str


def apply_opinion(proposal: TradeProposal, opinion: LLMOpinion | None, cfg: LLMSettings,
                  min_confidence: float) -> AdjustedConfidence:
    """Bounded effect of the LLM on a proposal. Exits are never blocked."""
    base = proposal.confidence
    if opinion is None:
        return AdjustedConfidence(base, False, "no LLM opinion")
    if proposal.intent == "exit":
        return AdjustedConfidence(base, False, "LLM ignored for exits")
    agrees = (opinion.bias == "bullish") if proposal.side == "buy" else (opinion.bias == "bearish")
    disagrees = (opinion.bias == "bearish") if proposal.side == "buy" else (opinion.bias == "bullish")
    delta = cfg.max_confidence_adjust * opinion.confidence
    if agrees:
        new = base + delta
    elif disagrees:
        new = base - delta
    else:
        new = base
    new = round(max(0.0, min(new, 1.0)), 3)
    if disagrees and opinion.confidence >= cfg.veto_confidence:
        return AdjustedConfidence(new, True, f"LLM veto: {opinion.bias} @ {opinion.confidence:.2f}")
    if new < min_confidence:
        return AdjustedConfidence(new, True, f"confidence {new} < {min_confidence} after LLM")
    return AdjustedConfidence(new, False, f"LLM {opinion.bias} @ {opinion.confidence:.2f}: {base} -> {new}")


class LLMAnalyst:
    def __init__(self, cfg: LLMSettings, api_key: str, client: Any | None = None):
        self.cfg = cfg
        if client is None:
            import anthropic  # optional dependency, only needed when LLM_ENABLED=true

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=cfg.timeout_s, max_retries=1)
        self._client = client

    async def analyze(self, summary: dict, proposal: TradeProposal) -> LLMOpinion | None:
        payload = {
            "market": summary,
            "proposed_trade": {
                "side": proposal.side,
                "setup": proposal.setup,
                "entry_price": str(proposal.price),
                "stop_loss": str(proposal.stop_loss),
                "take_profit": str(proposal.take_profit),
                "strategy_confidence": proposal.confidence,
                "strategy_reason": proposal.reason,
            },
        }
        try:
            return await asyncio.wait_for(self._call(payload), timeout=self.cfg.timeout_s)
        except asyncio.TimeoutError:
            log.warning("llm_timeout", pair=proposal.pair, timeout_s=self.cfg.timeout_s)
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            log.warning("llm_bad_output", pair=proposal.pair, error=str(e)[:300])
        except Exception as e:  # network/API errors must never crash the cycle
            log.warning("llm_error", pair=proposal.pair, error_type=type(e).__name__, error=str(e)[:300])
        return None

    async def _call(self, payload: dict) -> LLMOpinion | None:
        response = await self._client.beta.messages.create(
            model=self.cfg.model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, sort_keys=True)}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.effort,
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            log.warning("llm_refusal")
            return None
        if response.stop_reason == "max_tokens":
            raise ValueError("LLM output truncated (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ValueError("LLM returned no text block")
        return LLMOpinion.model_validate(json.loads(text))
