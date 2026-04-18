import hashlib
import re
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel


class MessageNormalizer(Skill):
    name = "message_normalizer"

    def __init__(self, policy: PolicyModel) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        preview = ctx.get("trigger_preview", "")
        channel = ctx.get("channel", "")
        author = ctx.get("author", "")

        normalized = re.sub(r"\s+", " ", preview).strip()
        fingerprint = hashlib.sha256(
            f"{channel}:{author}:{normalized}".encode()
        ).hexdigest()[:16]

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
