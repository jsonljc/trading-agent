import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.channel_policy_guard import ChannelPolicyGuard


def _policy(auto_execute: bool, channel: str = "mystic"):
    ch_cfg = MagicMock()
    ch_cfg.auto_execute = auto_execute
    p = MagicMock()
    p.watched_channels = {channel: ch_cfg}
    return p


def _store():
    s = MagicMock()
    s.update_policy_state = AsyncMock()
    return s


def _ctx(channel: str = "mystic", intent_id: str = "evt1:NVDA:long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": channel, "intent_id": intent_id})
    return ctx


async def test_auto_execute_true_passes():
    skill = ChannelPolicyGuard(_policy(auto_execute=True), _store())
    result = await skill.run(_ctx())
    assert result.status == "success"


async def test_auto_execute_false_blocks():
    store = _store()
    skill = ChannelPolicyGuard(_policy(auto_execute=False), store)
    ctx = _ctx()
    result = await skill.run(ctx)
    assert result.status == "skip"
    assert "channel_blocked" in result.reason
    store.update_policy_state.assert_called_once_with("evt1:NVDA:long", "channel_blocked")


async def test_unknown_channel_blocks():
    store = _store()
    skill = ChannelPolicyGuard(_policy(auto_execute=True, channel="mystic"), store)
    ctx = _ctx(channel="unknown-channel")
    result = await skill.run(ctx)
    assert result.status == "skip"
    store.update_policy_state.assert_called_once_with("evt1:NVDA:long", "channel_blocked")


async def test_no_intent_id_still_blocks():
    store = _store()
    skill = ChannelPolicyGuard(_policy(auto_execute=False), store)
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": "mystic"})
    result = await skill.run(ctx)
    assert result.status == "skip"
    store.update_policy_state.assert_not_called()
