from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class BootstrapReviewGate(Skill):
    """When trader is non-autonomous and the message is actionable, post a
    review digest and stop the pipeline (status=skip)."""

    name = "BootstrapReviewGate"

    def __init__(self, telegram_digest) -> None:
        self._digest = telegram_digest

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("trader_auto_execute", True):
            return SkillResult(status="success")
        if ctx.get("bucket") in (None, "SKIP"):
            return SkillResult(status="success")
        await self._digest.send_bootstrap_review_digest(ctx)
        return SkillResult(status="skip", reason="bootstrap_review_posted")
