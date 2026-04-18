import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from agent.context import Context
from agent.policy import PolicyModel
from skills.signal.desktop_reader import DesktopReader
import yaml


def make_policy():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    return PolicyModel.model_validate(yaml.safe_load(config_path.read_text()))


def make_ctx(preview: str, channel: str = "mystic", author: str = "Mystic") -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"trigger_preview": preview, "channel": channel, "author": author,
                 "full_message_text": preview, "capture_mode": "preview"})
    return ctx


async def test_skips_when_preview_is_complete():
    skill = DesktopReader(make_policy())
    ctx = make_ctx("Long $AVEX today's IPO, starting a position here")
    result = await skill.run(ctx)
    assert result.status == "skip"


async def test_does_not_skip_when_preview_is_truncated():
    skill = DesktopReader(make_policy())
    ctx = make_ctx("Long $AVEX today...")
    with patch.object(skill, "_capture_full_message", new=AsyncMock(return_value="Long $AVEX today full message")):
        result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates["full_message_text"] == "Long $AVEX today full message"
    assert result.updates["capture_mode"] == "desktop_reader"


async def test_fails_when_capture_fails():
    skill = DesktopReader(make_policy())
    ctx = make_ctx("Long $AVEX...")
    with patch.object(skill, "_capture_full_message", new=AsyncMock(side_effect=RuntimeError("screenshot failed"))):
        result = await skill.run(ctx)
    assert result.status == "fail"
    assert "screenshot failed" in result.reason
