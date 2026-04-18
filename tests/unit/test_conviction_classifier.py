import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.signal.conviction_classifier import ConvictionClassifier
from agent.policy import PolicyModel
import yaml


def make_policy():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    return PolicyModel.model_validate(yaml.safe_load(config_path.read_text()))


def make_ctx(text: str) -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"full_message_text": text, "ticker": "AVEX", "intent": "LONG_SIGNAL"})
    return ctx


def fake_response(bucket: str):
    mock = MagicMock()
    mock.content = [MagicMock(text=json.dumps({"conviction_bucket": bucket, "reason": "test"}))]
    return mock


async def test_high_conviction(monkeypatch):
    skill = ConvictionClassifier(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=fake_response("high")))
    result = await skill.run(make_ctx("Initiating a position, high conviction on this one"))
    assert result.status == "success"
    assert result.updates["conviction_bucket"] == "high"
    assert result.updates["target_allocation_pct"] == 0.10


async def test_low_conviction(monkeypatch):
    skill = ConvictionClassifier(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=fake_response("low")))
    result = await skill.run(make_ctx("Starting a small position here"))
    assert result.updates["conviction_bucket"] == "low"
    assert result.updates["target_allocation_pct"] == 0.05
