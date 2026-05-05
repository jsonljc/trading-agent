from __future__ import annotations
from agent.context import Context, SkillResult


def already_terminated(ctx: Context) -> SkillResult | None:
    """Options-leg skills no-op once a partial_execution_reason is set."""
    if ctx.get("partial_execution_reason"):
        return SkillResult(status="success")
    return None


def partial_or(ctx: Context, reason: str, fallback_status: str) -> SkillResult:
    """If shares have already filled, convert what would be a skip/fail in the
    options sub-chain into a partial success. Otherwise return the original
    terminal status so chains run without a prior shares fill still terminate.
    """
    if ctx.get("shares_intent_id"):
        return SkillResult(status="success",
                           updates={"partial_execution_reason": reason})
    return SkillResult(status=fallback_status, reason=reason)
