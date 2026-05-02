from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class EntrySkipGate(Skill):
    """Terminate the pipeline when the classifier produced no actionable entry.

    Runs AFTER ClassificationLogger so SKIP and low-confidence classifications
    are still recorded in classification_log before the pipeline halts.
    """

    name = "EntrySkipGate"

    async def run(self, ctx: Context) -> SkillResult:
        bucket = ctx.get("bucket")
        size_pct = ctx.get("size_pct", 0.0)
        if bucket in (None, "SKIP") or size_pct <= 0:
            return SkillResult(status="skip", reason=f"no_entry:bucket={bucket}")
        return SkillResult(status="success")
