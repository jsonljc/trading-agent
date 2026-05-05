from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class RthEntryGuard(Skill):
    name = "RthEntryGuard"

    async def run(self, ctx: Context) -> SkillResult:
        session = ctx.get("execution_session")
        if session != "rth":
            return SkillResult(status="skip",
                               reason=f"entry_outside_rth:{session}")
        return SkillResult(status="success")
