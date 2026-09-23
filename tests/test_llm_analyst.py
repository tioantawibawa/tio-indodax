import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.analysis.llm_analyst import LLMAnalyst, LLMOpinion, apply_opinion
from agent.config import LLMSettings
from tests.helpers import buy, sell


class FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return await self.behaviour()


def fake_client(behaviour):
    msgs = FakeMessages(behaviour)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="thinking", thinking=""),
                                                      SimpleNamespace(type="text", text=text)])


CFG = LLMSettings(timeout_s=0.2)
OK = json.dumps({"bias": "bullish", "confidence": 0.8, "reasoning": "r", "risks": ["x"]})


async def test_parses_valid_opinion_and_sends_structured_request():
    async def ok():
        return resp(OK)
    client, msgs = fake_client(ok)
    op = await LLMAnalyst(CFG, "k", client).analyze({"pair": "btc_idr"}, buy())
    assert op == LLMOpinion(bias="bullish", confidence=0.8, reasoning="r", risks=["x"])
    call = msgs.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"
    body = json.loads(call["messages"][0]["content"])
    assert body["market"] == {"pair": "btc_idr"} and "cash" not in json.dumps(body)


@pytest.mark.parametrize("behaviour", ["timeout", "error", "badjson", "badschema", "refusal", "truncated"])
async def test_failures_return_none_never_raise(behaviour):
    async def b():
        if behaviour == "timeout":
            await asyncio.sleep(5)
        if behaviour == "error":
            raise ConnectionError("down")
        if behaviour == "badjson":
            return resp("not json")
        if behaviour == "badschema":
            return resp(json.dumps({"bias": "moon", "confidence": 3}))
        if behaviour == "refusal":
            return resp("", stop="refusal")
        return resp(OK[:10], stop="max_tokens")
    client, _ = fake_client(b)
    assert await LLMAnalyst(CFG, "k", client).analyze({}, buy()) is None


def op(bias, conf):
    return LLMOpinion(bias=bias, confidence=conf, reasoning="", risks=[])


def test_apply_opinion_bounded():
    cfg = LLMSettings()  # max adjust 0.2, veto at 0.7
    p = replace(buy(), confidence=0.7)
    assert apply_opinion(p, op("bullish", 1.0), cfg, 0.55).confidence == pytest.approx(0.9)
    assert apply_opinion(p, op("neutral", 1.0), cfg, 0.55).confidence == 0.7
    down = apply_opinion(p, op("bearish", 0.5), cfg, 0.55)
    assert down.confidence == pytest.approx(0.6) and not down.vetoed
    assert apply_opinion(p, op("bearish", 0.7), cfg, 0.55).vetoed
    assert apply_opinion(p, None, cfg, 0.55).confidence == 0.7


def test_apply_opinion_below_min_confidence_vetoes():
    p = replace(buy(), confidence=0.6)
    r = apply_opinion(p, op("bearish", 0.6), LLMSettings(), 0.55)
    assert r.vetoed and r.confidence < 0.55


def test_llm_never_blocks_exits():
    r = apply_opinion(sell(emergency=True), op("bullish", 1.0), LLMSettings(), 0.55)
    assert not r.vetoed and r.confidence == 1.0


def test_llm_output_cannot_carry_orders():
    with pytest.raises(Exception):
        LLMOpinion.model_validate({"bias": "bullish", "confidence": 2, "reasoning": "", "risks": []})
    o = LLMOpinion.model_validate({"bias": "bullish", "confidence": 0.5, "reasoning": "", "risks": [],
                                   "qty": 100})  # extra fields ignored, never used
    assert not hasattr(o, "qty")
