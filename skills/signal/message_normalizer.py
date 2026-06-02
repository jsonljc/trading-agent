import hashlib
import re
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel


def compute_fingerprint(channel: str, author: str, text: str) -> str:
    """Stable 16-char fingerprint for a raw signal. Used by MessageNormalizer
    (for the idempotency key) and by main.py (for the signal_events row) so the
    two always match."""
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(
        f"{channel}:{author}:{normalized}".encode()
    ).hexdigest()[:16]


class MessageNormalizer(Skill):
    name = "message_normalizer"

    def __init__(self, policy: PolicyModel) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        preview = ctx.get("trigger_preview", "")
        channel = ctx.get("channel", "")
        author = ctx.get("author", "")

        normalized = re.sub(r"\s+", " ", preview).strip()
        fingerprint = compute_fingerprint(channel, author, preview)

        return SkillResult(
            status="success",
            updates={
                "trigger_preview": normalized,
                "full_message_text": normalized,
                "capture_mode": "preview",
                "message_fingerprint": fingerprint,
                "intent_timestamp": ctx.get("received_at", ""),
            },
        )
