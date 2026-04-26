import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.signal.signal_analyzer import SignalAnalyzer


def _policy():
    p = MagicMock()
    p.models.text = "claude-haiku-4-5-20251001"
    return p


def _ctx(text="Initiating long NVDA calls", channel="mystic", author="trader1"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({
        "full_message_text": text,
        "channel": channel,
        "author": author,
        "received_at": "2026-04-24T10:00:00+00:00",
    })
    return ctx


def _mock_response(json_text: str):
    content = MagicMock()
    content.text = json_text
    resp = MagicMock()
    resp.content = [content]
    return resp


async def test_valid_long_signal():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "long", '
        '"conviction": "high", "analysis_confidence": 0.95, "ambiguity_flags": [], '
        '"rationale": "Initiating long NVDA calls"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["ticker"] == "NVDA"
    assert result.updates["side"] == "long"
    assert result.updates["conviction"] == "high"
    assert result.updates["analysis_confidence"] == pytest.approx(0.95)
    assert result.updates["ambiguity_flags"] == "[]"


async def test_not_a_trade_signal_skips():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": false, "ticker": null, "side": "none", '
        '"conviction": "low", "analysis_confidence": 0.99, "ambiguity_flags": [], '
        '"rationale": "General market commentary"}'
    ))
    result = await skill.run(_ctx("Markets look interesting today"))
    assert result.status == "skip"


async def test_low_confidence_ambiguous():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "long", '
        '"conviction": "medium", "analysis_confidence": 0.55, '
        '"ambiguity_flags": ["direction_unclear"], "rationale": "Unclear signal"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "ambiguous_signal" in result.reason


async def test_ambiguity_flag_blocks_even_with_high_confidence():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "long", '
        '"conviction": "high", "analysis_confidence": 0.85, '
        '"ambiguity_flags": ["multiple_tickers_detected"], "rationale": "Two tickers"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "skip"


async def test_parse_failure_returns_fail():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response("not json"))
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "signal_parse_failed" in result.reason


async def test_invalid_side_enum_returns_fail():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "buy", '
        '"conviction": "high", "analysis_confidence": 0.9, "ambiguity_flags": [],'
        ' "rationale": "test"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "signal_parse_failed" in result.reason
