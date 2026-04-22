import pytest
import json
from datetime import datetime, timezone
from infra.storage.execution_store import ExecutionStore
from infra.ib.models import FillStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_insert_execution(db):
    store = ExecutionStore(db)
    await store.insert_execution({
        "id": "exec-1",
        "signal_id": "sig-1",
        "trace_id": "trace-1",
        "instrument_type": "option",
        "ticker": "AAPL",
        "contract_ref_json": json.dumps({"symbol": "AAPL"}),
        "quantity": 1,
        "notional_estimate": 500.0,
        "limit_price": 5.00,
        "sizing_reason": "high_conviction",
        "capped_by": None,
        "broker_order_id": None,
        "perm_id": None,
        "status": FillStatus.SUBMITTED_UNFILLED.value,
        "filled_qty": 0,
        "avg_fill_price": None,
        "idempotency_key": "trace-1:OrderSubmitter:sig-1",
        "submitted_at": _now(),
        "filled_at": None,
        "last_known_at": _now(),
    })
    async with db.execute("SELECT id, status FROM executions WHERE id='exec-1'") as cur:
        row = await cur.fetchone()
    assert row["id"] == "exec-1"
    assert row["status"] == "submitted_unfilled"


@pytest.mark.asyncio
async def test_update_execution_status(db):
    store = ExecutionStore(db)
    now = _now()
    await store.insert_execution({
        "id": "exec-2", "signal_id": "sig-2", "trace_id": "trace-2",
        "instrument_type": "equity", "ticker": "TSLA",
        "contract_ref_json": None, "quantity": 10, "notional_estimate": 2000.0,
        "limit_price": 200.0, "sizing_reason": "low_conviction", "capped_by": None,
        "broker_order_id": None, "perm_id": None,
        "status": FillStatus.SUBMITTED_UNFILLED.value,
        "filled_qty": 0, "avg_fill_price": None,
        "idempotency_key": "trace-2:OrderSubmitter:sig-2",
        "submitted_at": now, "filled_at": None, "last_known_at": now,
    })
    await store.update_execution_status(
        execution_id="exec-2",
        status=FillStatus.FILLED,
        filled_qty=10,
        avg_fill_price=201.5,
        broker_order_id="IB-999",
        perm_id=12345,
        filled_at=now,
    )
    async with db.execute("SELECT status, filled_qty, avg_fill_price FROM executions WHERE id='exec-2'") as cur:
        row = await cur.fetchone()
    assert row["status"] == "filled"
    assert row["filled_qty"] == 10
    assert row["avg_fill_price"] == 201.5


@pytest.mark.asyncio
async def test_insert_audit_log(db):
    store = ExecutionStore(db)
    await store.insert_audit_log({
        "id": "audit-1",
        "execution_id": "exec-1",
        "signal_id": "sig-1",
        "trace_id": "trace-1",
        "ctx_snapshot_json": json.dumps({"ticker": "AAPL"}),
        "pipeline_outcome": "success",
        "created_at": _now(),
    })
    async with db.execute("SELECT id FROM execution_audit_log WHERE id='audit-1'") as cur:
        row = await cur.fetchone()
    assert row["id"] == "audit-1"


@pytest.mark.asyncio
async def test_get_uncertain_executions(db):
    store = ExecutionStore(db)
    now = _now()
    for exec_id, status in [
        ("e1", FillStatus.SUBMITTED_UNFILLED.value),
        ("e2", FillStatus.TIMED_OUT_PENDING.value),
        ("e3", FillStatus.FILLED.value),
    ]:
        await store.insert_execution({
            "id": exec_id, "signal_id": "s", "trace_id": "t",
            "instrument_type": "equity", "ticker": "SPY",
            "contract_ref_json": None, "quantity": 1, "notional_estimate": 100.0,
            "limit_price": 100.0, "sizing_reason": "", "capped_by": None,
            "broker_order_id": f"ib-{exec_id}", "perm_id": None,
            "status": status, "filled_qty": 0, "avg_fill_price": None,
            "idempotency_key": f"k-{exec_id}",
            "submitted_at": now, "filled_at": None, "last_known_at": now,
        })
    rows = await store.get_uncertain_executions()
    ids = {r["id"] for r in rows}
    assert ids == {"e1", "e2"}
