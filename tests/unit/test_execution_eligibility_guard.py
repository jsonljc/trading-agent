import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
from agent.context import Context, SkillResult
from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
from infra.ib.models import ExecutionMode

ET = ZoneInfo("America/New_York")


def _policy(premarket=True, afterhours_queue=True):
    p = MagicMock()
    p.market_hours.rth_start = "09:30"
    p.market_hours.rth_end = "16:00"
    p.market_hours.stock_premarket_allowed = premarket
    p.market_hours.stock_premarket_start = "04:00"
    p.market_hours.stock_afterhours_queue = afterhours_queue
    return p


def _ctx():
    c = Context(trace_id="t1", event_id="e1")
    return c


def _at(hour, minute=0):
    return lambda: datetime(2026, 4, 22, hour, minute, tzinfo=ET)


@pytest.mark.asyncio
async def test_rth_execute_now():
    guard = ExecutionEligibilityGuard(_policy(), time_fn=_at(10, 0))
    result = await guard.run(_ctx())
    assert result.status == "success"
    assert result.updates["execution_mode"] == ExecutionMode.EXECUTE_NOW.value
    assert result.updates["execution_session"] == "rth"


@pytest.mark.asyncio
async def test_premarket_execute_now():
    guard = ExecutionEligibilityGuard(_policy(), time_fn=_at(6, 0))
    result = await guard.run(_ctx())
    assert result.status == "success"
    assert result.updates["execution_mode"] == ExecutionMode.EXECUTE_NOW.value
    assert result.updates["execution_session"] == "premarket"


@pytest.mark.asyncio
async def test_premarket_before_window_reject():
    guard = ExecutionEligibilityGuard(_policy(), time_fn=_at(3, 59))
    result = await guard.run(_ctx())
    assert result.status == "fail"
    assert "execution_ineligible" in result.reason


@pytest.mark.asyncio
async def test_afterhours_queue():
    guard = ExecutionEligibilityGuard(_policy(afterhours_queue=True), time_fn=_at(17, 0))
    result = await guard.run(_ctx())
    assert result.status == "success"
    assert result.updates["execution_mode"] == ExecutionMode.QUEUE_FOR_SESSION.value


@pytest.mark.asyncio
async def test_afterhours_no_queue_reject():
    guard = ExecutionEligibilityGuard(_policy(afterhours_queue=False), time_fn=_at(17, 0))
    result = await guard.run(_ctx())
    assert result.status == "fail"
