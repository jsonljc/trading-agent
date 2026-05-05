from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class ChannelPolicyGuard(Skill):
    name = "ChannelPolicyGuard"

    def __init__(self, policy, trade_intent_store) -> None:
        self._policy = policy  # retained for compat; no longer read here
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        # The trader profile (TraderRouter sets `trader_auto_execute`) is the
        # single source of truth — channel-level auto_execute is gone.
        if ctx.get("trader_auto_execute") is True:
            return SkillResult(status="success")

        intent_id = ctx.get("intent_id")
        channel = ctx.get("channel", "")
        trader = ctx.get("trader_handle", "?")
        reason = f"channel_blocked: trader '{trader}' (channel '{channel}') has auto_execute=False"
        logger.info("ChannelPolicyGuard: %s", reason)
        if intent_id:
            await self._store.update_policy_state(intent_id, "channel_blocked")
        return SkillResult(status="skip", reason=reason)
