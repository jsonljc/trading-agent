from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class ChannelPolicyGuard(Skill):
    name = "ChannelPolicyGuard"

    def __init__(self, policy, trade_intent_store) -> None:
        self._policy = policy
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        channel = ctx.get("channel", "")
        intent_id = ctx.get("intent_id")
        channel_cfg = self._policy.watched_channels.get(channel)

        if channel_cfg is None or not channel_cfg.auto_execute:
            reason = f"channel_blocked: channel '{channel}' has auto_execute=False or is not configured"
            logger.info("ChannelPolicyGuard: %s", reason)
            if intent_id:
                await self._store.update_policy_state(intent_id, "channel_blocked")
            return SkillResult(status="skip", reason=reason)

        return SkillResult(status="success")
