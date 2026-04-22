import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.fill_waiter import FillWaiter
from infra.ib.models import FillResult, FillStatus


def _fill_result(status: FillStatus, filled_qty=2, avg_price=5.25):
    return FillResult(
        status=status, broker_order_id="IB-1", perm_id=99,
        submitted_qty=2, filled_qty=filled_qty,
        remaining_qty=2-filled_qty, avg_fill_price=avg_price,
        last_status=status.value, status_timestamp="2026-04-22T10:00:00+00:00",
    )


def _ctx(broker_order_id="IB-1", execution_id="exec-1"):
    c = Context(trace_id="t", event_id="e")
    c.update({"broker_order_id": broker_order_id, "execution_id": execution_id,
               "_trade": MagicMock()})
    return c


@pytest.mark.asyncio
async def test_filled_returns_success(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.wait_fill = AsyncMock(return_value=_fill_result(FillStatus.FILLED))
    skill = FillWaiter(gw, store, timeout=1.0)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["fill_status"] == FillStatus.FILLED.value
    assert result.updates["filled_qty"] == 2


@pytest.mark.asyncio
async def test_timeout_returns_success_with_warning(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.wait_fill = AsyncMock(return_value=_fill_result(FillStatus.TIMED_OUT_PENDING, filled_qty=0))
    skill = FillWaiter(gw, store, timeout=1.0)
    result = await skill.run(_ctx())
    # Timeout is success — reconciler handles it
    assert result.status == "success"
    assert result.updates["fill_status"] == FillStatus.TIMED_OUT_PENDING.value


@pytest.mark.asyncio
async def test_rejected_returns_fail(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.wait_fill = AsyncMock(return_value=_fill_result(FillStatus.REJECTED, filled_qty=0, avg_price=None))
    skill = FillWaiter(gw, store, timeout=1.0)
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "rejected" in result.reason
