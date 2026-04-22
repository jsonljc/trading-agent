import pytest
import json
from agent.context import Context
from skills.execution.execution_audit_writer import ExecutionAuditWriter


def _ctx(execution_id="exec-1", signal_id="sig-1"):
    c = Context(trace_id="trace-1", event_id=signal_id)
    c.update({"signal_id": signal_id, "execution_id": execution_id, "ticker": "AAPL"})
    return c


@pytest.mark.asyncio
async def test_audit_writer_inserts_snapshot(db):
    writer = ExecutionAuditWriter(db)
    await writer.write(ctx=_ctx(), pipeline_outcome="success")
    async with db.execute("SELECT * FROM execution_audit_log") as cur:
        row = await cur.fetchone()
    assert row["trace_id"] == "trace-1"
    assert row["pipeline_outcome"] == "success"
    snapshot = json.loads(row["ctx_snapshot_json"])
    assert snapshot["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_audit_writer_records_failure_outcome(db):
    writer = ExecutionAuditWriter(db)
    await writer.write(ctx=_ctx(), pipeline_outcome="failed")
    async with db.execute("SELECT pipeline_outcome FROM execution_audit_log") as cur:
        row = await cur.fetchone()
    assert row["pipeline_outcome"] == "failed"
