from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from skills.execution._options_leg import already_terminated, partial_or


class InstrumentMarketabilityGuard(Skill):
    name = "InstrumentMarketabilityGuard"

    def __init__(self, policy) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        if (r := already_terminated(ctx)):
            return r
        session = ctx.get("execution_session", "rth")
        candidates = ctx.get("option_candidates", [])
        max_spread = self._policy.pricing_policy_guards.max_spread_pct

        if session != "rth":
            return partial_or(ctx, "options_outside_rth", "skip")
        if not candidates:
            return partial_or(ctx, "no_option_candidates", "skip")
        viable = [c for c in candidates if c.spread_pct <= max_spread]
        if not viable:
            return partial_or(ctx, "all_candidates_spread_too_wide", "skip")

        return SkillResult(status="success", updates={"instrument_type": "option"})
