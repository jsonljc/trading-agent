from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class InstrumentMarketabilityGuard(Skill):
    name = "InstrumentMarketabilityGuard"

    def __init__(self, policy) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        session = ctx.get("execution_session", "rth")
        candidates = ctx.get("option_candidates", [])
        max_spread = self._policy.pricing_policy_guards.max_spread_pct

        if session != "rth":
            return SkillResult(status="success", updates={
                "instrument_type": "equity",
                "fallback_reason": "options_outside_rth",
            })

        if not candidates:
            return SkillResult(status="success", updates={
                "instrument_type": "equity",
                "fallback_reason": "no_option_candidates",
            })

        viable = [c for c in candidates if c.spread_pct <= max_spread]
        if not viable:
            return SkillResult(status="success", updates={
                "instrument_type": "equity",
                "fallback_reason": "all_candidates_spread_too_wide",
            })

        return SkillResult(status="success", updates={
            "instrument_type": "option",
            "fallback_reason": None,
        })
