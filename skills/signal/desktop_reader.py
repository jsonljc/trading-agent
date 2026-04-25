from __future__ import annotations
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

logger = logging.getLogger(__name__)

_AX_MIN_LENGTH = 40
_SCREENSHOT_TIMEOUT_S = 1.0
_NAV_PATTERNS = ("Stock Talk Insiders", "丨", "#")


def _passes_ax_validation(text: str) -> bool:
    if len(text.strip()) < _AX_MIN_LENGTH:
        return False
    for pattern in _NAV_PATTERNS:
        if pattern in text:
            return False
    return True


class DesktopReader(Skill):
    name = "desktop_reader"

    def __init__(self, policy) -> None:
        self._policy = policy
        self._client = anthropic.AsyncAnthropic()

    async def run(self, ctx: Context) -> SkillResult:
        preview = ctx.get("full_message_text", ctx.get("trigger_preview", ""))

        if _passes_ax_validation(preview):
            return SkillResult(status="success", updates={
                "full_message_text": preview,
                "capture_mode": "ax",
            })

        try:
            text, mode = await self._bounded_screenshot_extract(preview)
            return SkillResult(status="success", updates={
                "full_message_text": text,
                "capture_mode": mode,
            })
        except Exception as exc:
            logger.warning("DesktopReader: screenshot fallback failed (%s) — using preview", exc)
            return SkillResult(status="success", updates={
                "full_message_text": preview,
                "capture_mode": "preview_fallback",
            })

    async def _bounded_screenshot_extract(self, fallback_text: str) -> tuple[str, str]:
        loop = asyncio.get_event_loop()
        try:
            image_data = await asyncio.wait_for(
                loop.run_in_executor(None, self._capture_message_pane),
                timeout=_SCREENSHOT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise Exception("screenshot_timeout")

        b64 = base64.standard_b64encode(image_data).decode()
        response = await asyncio.wait_for(
            self._client.messages.create(
                model=self._policy.models.text,
                max_tokens=512,
                system="Extract the Discord message text verbatim. Return only the message text.",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                        {"type": "text", "text": "Extract all Discord message text you can see."},
                    ],
                }],
            ),
            timeout=_SCREENSHOT_TIMEOUT_S,
        )
        return response.content[0].text.strip(), "screenshot"

    def _capture_message_pane(self) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            subprocess.run(["screencapture", "-x", path], check=True, capture_output=True)
            return Path(path).read_bytes()
        finally:
            import os
            try:
                os.unlink(path)
            except OSError:
                pass
