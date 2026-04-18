import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.signal.trade_intent_detector import TradeIntentDetector
from agent.policy import PolicyModel
import yaml


def make_policy():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    return PolicyModel.model_validate(yaml.safe_load(config_path.read_text()))


def make_ctx(text: str) -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"full_message_text": text, "channel": "mystic", "author": "Mystic"})
    return ctx


def fake_claude_response(intent: str, confidence: str = "high", reason: str = "test"):
    mock = MagicMock()
    mock.content = [MagicMock(text=json.dumps({
        "intent": intent, "confidence": confidence, "reason": reason
    }))]
    return mock


async def test_detects_long_signal(monkeypatch):
    skill = TradeIntentDetector(make_policy())
    monkeypatch.setattr(
        skill._client.messages, "create",
        AsyncMock(return_value=fake_claude_response("LONG_SIGNAL"))
    )
    result = await skill.run(make_ctx("Long $AVEX today's IPO"))
    assert result.status == "success"
    assert result.updates["intent"] == "LONG_SIGNAL"
    assert result.updates["confidence"] == "high"


async def test_no_action_returns_skip(monkeypatch):
    skill = TradeIntentDetector(make_policy())
    monkeypatch.setattr(
        skill._client.messages, "create",
        AsyncMock(return_value=fake_claude_response("NO_ACTION"))
    )
    result = await skill.run(make_ctx("Interesting setup but just watching"))
    assert result.status == "skip"
    assert "NO_ACTION" in result.reason


async def test_watchlist_returns_skip(monkeypatch):
    skill = TradeIntentDetector(make_policy())
    monkeypatch.setattr(
        skill._client.messages, "create",
        AsyncMock(return_value=fake_claude_response("WATCHLIST_ONLY"))
    )
    result = await skill.run(make_ctx("Watching $AVEX closely"))
    assert result.status == "skip"
