from __future__ import annotations
import logging
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class TradeIntentWriter(Skill):
    name = "TradeIntentWriter"

    def __init__(self, trade_intent_store) -> None:
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        if not ticker:
            return SkillResult(status="fail", reason="trade_intent_writer: ticker missing from context")

        side = ctx.get("side")
        if not side:
            phase1_intent = ctx.get("intent", "")
            if phase1_intent in ("LONG_SIGNAL", "ADD_SIGNAL"):
                side = "long"
            else:
                # Fail loudly rather than coerce: a SHORT_SIGNAL silently
                # written as long would open a position in the wrong direction.
                return SkillResult(
                    status="fail",
                    reason=f"trade_intent_writer: unknown intent {phase1_intent!r}; expected LONG_SIGNAL or ADD_SIGNAL",
                )

        conviction = ctx.get("bucket") or ctx.get("conviction", "LOW")

        now = datetime.now(timezone.utc).isoformat()
        intent_id = f"{ctx.event_id}:{ticker}:{side}"

        record = {
            "intent_id": intent_id,
            "event_id": ctx.event_id,
            "channel": ctx.get("channel", ""),
            "ticker": ticker,
            "side": side,
            "instrument_type": ctx.get("instrument_type", "equity"),
            "parent_intent_id": ctx.get("parent_intent_id"),
            "expiry": None,
            "strike": None,
            "right": None,
            "conviction": conviction,
            "analysis_confidence": ctx.get("analysis_confidence"),
            "ambiguity_flags": ctx.get("ambiguity_flags"),
            "rationale": ctx.get("reason"),
            "ticker_raw": ctx.get("ticker_raw", ticker),
            "side_raw": ctx.get("side_raw") or ctx.get("intent"),
            "conviction_raw": ctx.get("conviction_raw") or ctx.get("bucket"),
            "reference_spot_price": None,
            "reference_spot_timestamp": None,
            "policy_state": "approved",
            "execution_mode": None,
            "execution_state": None,
            # Left None until the shares write-ahead sets 'dispatched' at submit.
            # Seeding 'pending' here would strand every guard-skipped intent in
            # the in-flight set forever (nothing transitions a never-submitted
            # row out of 'pending'); the reconciler only acts on rows that carry
            # a broker_order_ref, so 'pending' added no value.
            "outbox_status": None,
            "signal_received_at": ctx.get("received_at", now),
            "intent_created_at": now,
            "created_at": now,
            "updated_at": now,
        }

        await self._store.insert(record)
        ctx.update({"intent_id": intent_id})
        logger.info("TradeIntentWriter: created intent %s for %s/%s", intent_id, ticker, side)
        return SkillResult(status="success", updates={"intent_id": intent_id})
