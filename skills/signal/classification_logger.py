from __future__ import annotations
import json
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.storage.classification_log_store import ClassificationLogStore


logger = logging.getLogger(__name__)


class ClassificationLogger(Skill):
    name = "ClassificationLogger"

    def __init__(self, store: ClassificationLogStore) -> None:
        self._store = store

    async def run(self, ctx: Context) -> SkillResult:
        bucket = ctx.get("bucket")
        if bucket is None:
            return SkillResult(status="success")  # nothing classified — earlier skip

        size_pct = ctx.get("size_pct", 0.0)
        size_source = ctx.get("size_source", "skip")
        confidence = float(ctx.get("confidence", 0.0))
        action_taken = self._infer_action(ctx, bucket)

        features_json = ctx.get("classifier_features_json", "{}")
        llm_json = ctx.get("classifier_llm_response_json")

        try:
            await self._store.insert(
                event_id=ctx.event_id,
                trader_handle=ctx.get("trader_handle", "unknown"),
                msg_text=ctx.get("full_message_text", ""),
                features=json.loads(features_json) if features_json else {},
                llm_response=json.loads(llm_json) if llm_json else None,
                bucket=bucket, confidence=confidence,
                size_pct=size_pct, size_source=size_source,
                action_taken=action_taken,
                reason=ctx.get("classifier_reason", ""),
            )
        except Exception as exc:
            logger.exception("classification_logger failed: %s", exc)
        return SkillResult(status="success")

    @staticmethod
    def _infer_action(ctx: Context, bucket: str) -> str:
        size_source = ctx.get("size_source")
        if size_source == "drop_low_conf":
            return "dropped_low_conf"
        if size_source == "llm_error":
            return "llm_error"
        if size_source == "ticker_not_in_msg":
            return "ticker_not_in_msg"
        if bucket in (None, "SKIP"):
            return "skipped"
        return "fired"
