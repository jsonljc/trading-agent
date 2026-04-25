import asyncio
import base64
import logging
import subprocess
import tempfile
import time
from pathlib import Path

import anthropic

from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel

logger = logging.getLogger(__name__)

_TRUNCATION_MARKERS = ("...", "…")
_MIN_COMPLETE_LENGTH = 30


class DesktopReader(Skill):
    name = "desktop_reader"

    def __init__(self, policy: PolicyModel) -> None:
        self._policy = policy
        self._client = anthropic.AsyncAnthropic()

    def _is_preview_complete(self, preview: str) -> bool:
        stripped = preview.strip()
        if len(stripped) < _MIN_COMPLETE_LENGTH:
            return False
        if any(stripped.endswith(m) for m in _TRUNCATION_MARKERS):
            return False
        return True

    async def run(self, ctx: Context) -> SkillResult:
        preview = ctx.get("full_message_text", ctx.get("trigger_preview", ""))
        if self._is_preview_complete(preview):
            return SkillResult(status="success")

        channel = ctx.get("channel", "")
        author = ctx.get("author", "")
        try:
            full_text = await self._capture_full_message(channel, author)
            return SkillResult(
                status="success",
                updates={"full_message_text": full_text, "capture_mode": "desktop_reader"},
            )
        except Exception as exc:
            logger.exception("desktop_reader capture failed")
            return SkillResult(status="fail", reason=f"desktop_reader failed: {exc}")

    async def _capture_full_message(self, channel: str, author: str) -> str:
        loop = asyncio.get_event_loop()
        image_data = await loop.run_in_executor(None, self._navigate_and_screenshot, channel)
        b64 = base64.standard_b64encode(image_data).decode()

        response = await self._client.messages.create(
            model=self._policy.models.vision,
            max_tokens=2048,
            system="Extract Discord message text verbatim and completely. Include all lines, bullet points, tickers, and formatting. Return only the message text, no commentary.",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": f"Extract ALL text visible in the Discord message area — every line, ticker, percentage, cost basis, section header. Transcribe everything you can read verbatim top to bottom. Do not summarize or skip anything."},
                ],
            }],
        )
        return response.content[0].text.strip()

    def _navigate_and_screenshot(self, channel: str) -> bytes:
        # Activate Discord
        subprocess.run(["osascript", "-e", 'tell application "Discord" to activate'],
                       capture_output=True)
        time.sleep(1.5)

        # Navigate to channel via quick switcher, then scroll down to latest content
        nav_script = f'''
tell application "System Events"
    tell process "Discord"
        key code 53
        delay 0.3
        keystroke "k" using command down
        delay 1.0
        keystroke "{channel}"
        delay 1.0
        key code 125
        delay 0.5
        key code 36
        delay 2.0
        set winPos to position of window 1
        set winSize to size of window 1
        set clickX to (item 1 of winPos) + (item 1 of winSize) / 2
        set clickY to (item 2 of winPos) + (item 2 of winSize) / 2
        click at {{clickX, clickY}}
        delay 0.3
        repeat 15 times
            key code 121
            delay 0.1
        end repeat
        delay 1.0
    end tell
end tell
'''
        subprocess.run(["osascript", "-e", nav_script], capture_output=True)
        time.sleep(0.5)

        # Screenshot immediately — Discord is still frontmost
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            screenshot_path = f.name
        try:
            subprocess.run(["screencapture", "-x", screenshot_path], check=True, capture_output=True)
            return Path(screenshot_path).read_bytes()
        finally:
            import os
            try:
                os.unlink(screenshot_path)
            except OSError:
                pass
