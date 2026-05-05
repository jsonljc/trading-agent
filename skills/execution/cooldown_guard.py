from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class CooldownGuard(Skill):
    name = "CooldownGuard"

    def __init__(self, policy, trade_intent_store) -> None:
        self._policy = policy
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        # Note: this guard is a read-then-skip check, not a transactional
        # claim. Two concurrent signals for the same ticker that both arrive
        # before either fills will both pass — accepted as a rare edge case.
        cp = self._policy.cooldown_policy
        if not cp.enabled:
            return SkillResult(status="success")

        ticker = ctx.get("ticker", "")
        intent_id = ctx.get("intent_id")
        since = (
            datetime.now(timezone.utc) - timedelta(minutes=cp.cooldown_minutes)
        ).isoformat()

        recent_fills = await self._store.get_filled_since(ticker, since)
        if recent_fills:
            reason = (
                f"cooldown_blocked: filled {ticker} within last {cp.cooldown_minutes}m"
            )
            logger.info("CooldownGuard: %s", reason)
            if intent_id:
                await self._store.update_policy_state(intent_id, "cooldown_blocked")
            return SkillResult(status="skip", reason=reason)

        return SkillResult(status="success")
