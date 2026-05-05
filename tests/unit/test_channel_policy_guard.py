import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.channel_policy_guard import ChannelPolicyGuard


def _store():
    s = MagicMock()
    s.update_policy_state = AsyncMock()
    return s


def _ctx(*, trader_auto_execute: bool, channel: str = "mystic",
         intent_id: str = "evt1:NVDA:long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({
        "channel": channel,
        "intent_id": intent_id,
        "trader_handle": "mystic",
        "trader_auto_execute": trader_auto_execute,
    })
    return ctx


async def test_trader_auto_execute_true_passes():
    skill = ChannelPolicyGuard(MagicMock(), _store())
    result = await skill.run(_ctx(trader_auto_execute=True))
    assert result.status == "success"


async def test_trader_auto_execute_false_blocks():
    store = _store()
    skill = ChannelPolicyGuard(MagicMock(), store)
    result = await skill.run(_ctx(trader_auto_execute=False))
    assert result.status == "skip"
    assert "channel_blocked" in result.reason
    store.update_policy_state.assert_called_once_with("evt1:NVDA:long", "channel_blocked")


async def test_missing_trader_auto_execute_blocks():
    store = _store()
    skill = ChannelPolicyGuard(MagicMock(), store)
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": "mystic", "intent_id": "evt1:NVDA:long"})
    result = await skill.run(ctx)
    assert result.status == "skip"
    store.update_policy_state.assert_called_once()


async def test_no_intent_id_still_blocks():
    store = _store()
    skill = ChannelPolicyGuard(MagicMock(), store)
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": "mystic", "trader_auto_execute": False})
    result = await skill.run(ctx)
    assert result.status == "skip"
    store.update_policy_state.assert_not_called()
