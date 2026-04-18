import asyncio
import base64
import logging
import subprocess
import tempfile
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
        subprocess.run(["osascript", "-e", 'tell application "Discord" to activate'],
                       check=True, capture_output=True)
        await asyncio.sleep(1.5)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            screenshot_path = f.name
        try:
            subprocess.run(["screencapture", "-x", screenshot_path], check=True, capture_output=True)
            image_data = Path(screenshot_path).read_bytes()
        finally:
            import os
            try:
                os.unlink(screenshot_path)
            except OSError:
                pass
        b64 = base64.standard_b64encode(image_data).decode()

        response = await self._client.messages.create(
            model=self._policy.models.vision,
            max_tokens=512,
            system="Extract the latest Discord message text verbatim. Return only the message text, no commentary.",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": f"What is the latest message in the #{channel} channel from {author}? Return only the message text."},
                ],
            }],
        )
        return response.content[0].text.strip()
