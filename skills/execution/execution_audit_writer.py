from __future__ import annotations
import dataclasses
import json
import uuid
import logging
from datetime import datetime, timezone
from agent.context import Context

logger = logging.getLogger(__name__)


def _json_default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "dict") and callable(o.dict):
        try:
            return o.dict()
        except Exception:
            pass
    return repr(o)


class ExecutionAuditWriter:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def write(self, ctx: Context, pipeline_outcome: str) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO execution_audit_log
               (id, execution_id, signal_id, trace_id,
                ctx_snapshot_json, pipeline_outcome, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                ctx.get("execution_id"),
                ctx.get("signal_id", ctx.event_id),
                ctx.trace_id,
                json.dumps(dict(ctx.data), default=_json_default),
                pipeline_outcome,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._conn.commit()
        logger.debug("ExecutionAuditWriter: wrote audit log for trace=%s outcome=%s",
                     ctx.trace_id, pipeline_outcome)

    async def update_intent_outbox_status(self, intent_id: str, outbox_status: str) -> None:
        await self._conn.execute(
            "UPDATE trade_intents SET outbox_status=?, updated_at=? WHERE intent_id=?",
            (outbox_status, datetime.now(timezone.utc).isoformat(), intent_id),
        )
        await self._conn.commit()
        logger.debug("ExecutionAuditWriter: intent %s outbox_status=%s", intent_id, outbox_status)
