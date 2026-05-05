import pytest
from agent.context import Context
from skills.execution.rth_entry_guard import RthEntryGuard


def _ctx(session: str) -> Context:
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"execution_session": session})
    return ctx


@pytest.mark.asyncio
async def test_passes_during_rth():
    g = RthEntryGuard()
    result = await g.run(_ctx("rth"))
    assert result.status == "success"


@pytest.mark.asyncio
async def test_skips_premarket():
    g = RthEntryGuard()
    result = await g.run(_ctx("premarket"))
    assert result.status == "skip"
    assert "rth" in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_skips_afterhours():
    g = RthEntryGuard()
    result = await g.run(_ctx("afterhours"))
    assert result.status == "skip"
