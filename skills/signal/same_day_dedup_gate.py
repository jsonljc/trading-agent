from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.storage.classification_log_store import ClassificationLogStore


logger = logging.getLogger(__name__)


# Window matches an aggressive "same trading session" semantic. STW's
# teaser-then-DD pattern can span ~12 hours (teaser at 10:45 PM ET, DD the
# next morning). 24h covers same-day repeats plus modest overnight slack.
DEFAULT_DEDUP_WINDOW_HOURS = 24.0


class SameDayDedupGate(Skill):
    """Drop a fired classification if (trader, ticker, side) already fired
    within the configured window.

    Runs AFTER ClassificationLogger so the duplicate gets recorded — we want
    visibility into "this would have double-fired" without acting on it.
    Sets bucket=SKIP so EntrySkipGate halts the pipeline naturally.
    """

    name = "SameDayDedupGate"

    def __init__(self, store: ClassificationLogStore,
                 window_hours: float = DEFAULT_DEDUP_WINDOW_HOURS) -> None:
        self._store = store
        self._window_hours = window_hours

    async def run(self, ctx: Context) -> SkillResult:
        bucket = ctx.get("bucket")
        if bucket not in ("HIGH", "LOW"):
            return SkillResult(status="success")  # not an actionable entry
        trader = ctx.get("trader_handle")
        ticker = ctx.get("ticker")
        side = ctx.get("side")
        if not (trader and ticker and side):
            return SkillResult(status="success")

        fired = await self._store.has_fired_recently(
            trader_handle=trader, ticker=ticker, side=side,
            hours=self._window_hours,
        )
        if not fired:
            return SkillResult(status="success")

        reason = (f"same_day_dedup: {trader}/{ticker}/{side} already fired "
                  f"within {self._window_hours:g}h")
        logger.info("SameDayDedupGate: %s", reason)
        updates = {
            "bucket": "SKIP",
            "size_pct": 0.0,
            "size_source": "dedup",
            "classifier_reason": reason,
        }
        ctx.update(updates)
        return SkillResult(status="skip", updates=updates, reason=reason)
