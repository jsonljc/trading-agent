import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.signal.ticker_resolver import TickerResolver
from agent.policy import PolicyModel
import yaml


def make_policy():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    return PolicyModel.model_validate(yaml.safe_load(config_path.read_text()))


def make_ctx(text: str) -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"full_message_text": text, "intent": "LONG_SIGNAL"})
    return ctx


def fake_response(ticker: str | None, ambiguous: bool = False):
    mock = MagicMock()
    payload = {"ticker": ticker, "ambiguous": ambiguous, "asset_type_hint": "equity"}
    mock.content = [MagicMock(text=json.dumps(payload))]
    return mock


async def test_resolves_dollar_ticker(monkeypatch):
    skill = TickerResolver(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=fake_response("AVEX")))
    result = await skill.run(make_ctx("Long $AVEX today"))
    assert result.status == "success"
    assert result.updates["ticker"] == "AVEX"


async def test_resolves_spelled_out_ticker(monkeypatch):
    skill = TickerResolver(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=fake_response("MITK")))
    result = await skill.run(make_ctx("Long ticker M-I-T-K"))
    assert result.updates["ticker"] == "MITK"


async def test_ambiguous_ticker_fails(monkeypatch):
    skill = TickerResolver(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=fake_response(None, ambiguous=True)))
    result = await skill.run(make_ctx("Long the AI names"))
    assert result.status == "fail"
    assert "ambiguous" in result.reason.lower()
