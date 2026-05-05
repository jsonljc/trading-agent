from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import ExecutionPolicy


class SizingResolver(Skill):
    name = "SizingResolver"

    def __init__(self, execution_policy: ExecutionPolicy) -> None:
        self._policy = execution_policy

    async def run(self, ctx: Context) -> SkillResult:
        bucket = ctx.get("bucket")
        if bucket == "SKIP" or bucket is None:
            return SkillResult(status="skip", reason=f"sizing_resolver: bucket={bucket}")
        channel = ctx.get("channel")
        sz = self._policy.sizing
        buckets = sz.per_channel.get(channel, sz.default)
        tier = buckets.high if bucket == "HIGH" else buckets.low
        updates = {"shares_pct": tier.shares, "options_pct": tier.options}
        ctx.update(updates)
        return SkillResult(status="success", updates=updates)
