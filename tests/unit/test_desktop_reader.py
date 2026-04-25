import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.signal.desktop_reader import DesktopReader


def _policy():
    p = MagicMock()
    p.models.text = "claude-haiku-4-5-20251001"
    p.models.vision = "claude-opus-4-7"
    return p


def _ctx(preview: str = "", source: str = "reconciliation"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({
        "full_message_text": preview,
        "trigger_preview": preview,
        "channel": "mystic",
        "capture_mode": source,
    })
    return ctx


async def test_valid_ax_text_skips_screenshot():
    """Long clean AX text passes validation gate — no screenshot taken."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock()
    ctx = _ctx("Initiating a long position in NVDA calls high conviction entry here today")
    result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates.get("capture_mode") == "ax"
    skill._bounded_screenshot_extract.assert_not_called()


async def test_nav_pattern_triggers_screenshot_fallback():
    """Text matching nav chrome triggers bounded screenshot fallback."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock(return_value=(
        "Initiating long NVDA calls here with strong conviction", "screenshot"
    ))
    ctx = _ctx("Stock Talk Insiders 丨 mystic")
    result = await skill.run(ctx)
    skill._bounded_screenshot_extract.assert_called_once()
    assert result.updates.get("capture_mode") in ("screenshot", "preview_fallback")


async def test_short_text_triggers_screenshot_fallback():
    """Text shorter than 40 chars triggers fallback."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock(return_value=(
        "Long NVDA calls initiating position today with strong conviction", "screenshot"
    ))
    ctx = _ctx("short text")
    result = await skill.run(ctx)
    skill._bounded_screenshot_extract.assert_called_once()


async def test_screenshot_timeout_uses_preview_fallback():
    """If screenshot extraction raises, use preview text as fallback."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock(side_effect=Exception("timeout"))
    ctx = _ctx("Short preview text")
    result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates.get("capture_mode") == "preview_fallback"
