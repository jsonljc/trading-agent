from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.traders.registry import TraderRegistry


class TraderRouter(Skill):
    name = "TraderRouter"

    def __init__(self, registry: TraderRegistry) -> None:
        self._registry = registry

    async def run(self, ctx: Context) -> SkillResult:
        author = ctx.get("author", "")

        if self._registry.is_bot_author(author):
            return SkillResult(status="skip", reason=f"bot_author:{author}")

        profile = self._registry.lookup(author)
        if profile is None:
            return SkillResult(status="skip", reason=f"no_trader_profile:{author}")

        msg = ctx.get("full_message_text", "")
        if profile.require_alert_mention and profile.alert_mention not in msg:
            return SkillResult(
                status="skip",
                reason=f"missing_alert_mention:{profile.alert_mention}",
            )

        updates = {
            "trader_handle": profile.handle,
            "trader_auto_execute": profile.auto_execute,
        }
        ctx.update(updates)
        return SkillResult(status="success", updates=updates)
