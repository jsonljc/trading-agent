import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.cooldown_guard import CooldownGuard


def _policy(enabled: bool = True, cooldown_minutes: int = 30):
    p = MagicMock()
    p.cooldown_policy.enabled = enabled
    p.cooldown_policy.cooldown_minutes = cooldown_minutes
    return p


def _store(filled_rows=None):
    s = MagicMock()
    s.get_filled_since = AsyncMock(return_value=filled_rows or [])
    s.update_policy_state = AsyncMock()
    return s


def _ctx(ticker="NVDA", intent_id="evt1:NVDA:long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"ticker": ticker, "intent_id": intent_id})
    return ctx


async def test_no_recent_fill_passes():
    store = _store(filled_rows=[])
    skill = CooldownGuard(_policy(), store)
    result = await skill.run(_ctx())
    assert result.status == "success"


async def test_recent_fill_blocks():
    store = _store(filled_rows=[{"ticker": "NVDA", "filled_at": "2026-04-24T10:00:00+00:00"}])
    skill = CooldownGuard(_policy(), store)
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "cooldown_blocked" in result.reason
    store.update_policy_state.assert_called_once_with("evt1:NVDA:long", "cooldown_blocked")


async def test_disabled_policy_always_passes():
    store = _store(filled_rows=[{"ticker": "NVDA", "filled_at": "2026-04-24T10:00:00+00:00"}])
    skill = CooldownGuard(_policy(enabled=False), store)
    result = await skill.run(_ctx())
    assert result.status == "success"
    store.get_filled_since.assert_not_called()


async def test_different_ticker_not_affected():
    store = _store(filled_rows=[])
    skill = CooldownGuard(_policy(), store)
    result = await skill.run(_ctx(ticker="AAPL"))
    assert result.status == "success"
    call_ticker = store.get_filled_since.call_args[0][0]
    assert call_ticker == "AAPL"
