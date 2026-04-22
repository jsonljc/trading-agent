from __future__ import annotations
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import ExecutionMode

ET = ZoneInfo("America/New_York")


def _default_time_fn() -> datetime:
    return datetime.now(ET)


def _parse_time(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class ExecutionEligibilityGuard(Skill):
    name = "ExecutionEligibilityGuard"

    def __init__(self, policy, time_fn=None) -> None:
        self._policy = policy
        self._time_fn = time_fn or _default_time_fn

    async def run(self, ctx: Context) -> SkillResult:
        mh = self._policy.market_hours
        now = self._time_fn()
        current = now.time().replace(second=0, microsecond=0)

        rth_start = _parse_time(mh.rth_start)
        rth_end = _parse_time(mh.rth_end)
        premarket_start = _parse_time(mh.stock_premarket_start)

        if rth_start <= current < rth_end:
            return SkillResult(status="success", updates={
                "execution_mode": ExecutionMode.EXECUTE_NOW.value,
                "execution_session": "rth",
            })

        if mh.stock_premarket_allowed and premarket_start <= current < rth_start:
            return SkillResult(status="success", updates={
                "execution_mode": ExecutionMode.EXECUTE_NOW.value,
                "execution_session": "premarket",
            })

        if current >= rth_end:
            if mh.stock_afterhours_queue:
                return SkillResult(status="success", updates={
                    "execution_mode": ExecutionMode.QUEUE_FOR_SESSION.value,
                    "execution_session": "afterhours",
                })
            return SkillResult(
                status="fail",
                reason=f"execution_ineligible: afterhours queue disabled (current ET {current})",
            )

        return SkillResult(
            status="fail",
            reason=f"execution_ineligible: outside all eligible windows (current ET {current})",
        )
