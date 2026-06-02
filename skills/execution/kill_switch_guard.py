from __future__ import annotations
import logging
import os
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class KillSwitchGuard(Skill):
    """Operator emergency stop for NEW entries.

    If a sentinel file exists on disk, halt the execution chain before any order
    is placed. This is a human kill switch (`touch data/KILL` to engage, `rm` to
    release) — it does NOT touch held positions or the trim ladder, and it is not
    an automated risk exit. It complements, and does not replace, the deliberate
    no-auto-kill design: downside is still handled by following the trader.
    """

    name = "KillSwitchGuard"

    def __init__(self, sentinel_path: str) -> None:
        self._path = sentinel_path

    async def run(self, ctx: Context) -> SkillResult:
        if os.path.exists(self._path):
            logger.warning(
                "KillSwitchGuard: sentinel %s present — halting NEW entry for "
                "%s (trace %s)", self._path, ctx.get("ticker") or "?", ctx.trace_id,
            )
            return SkillResult(status="skip", reason="kill_switch_engaged")
        return SkillResult(status="success")
