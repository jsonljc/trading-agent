from __future__ import annotations
import json
import uuid
import logging
from datetime import datetime, timezone
from agent.context import Context

logger = logging.getLogger(__name__)


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
                json.dumps(dict(ctx.data)),
                pipeline_outcome,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._conn.commit()
        logger.debug("ExecutionAuditWriter: wrote audit log for trace=%s outcome=%s",
                     ctx.trace_id, pipeline_outcome)
