import pytest
from agent.context import Context
from skills.execution.kill_switch_guard import KillSwitchGuard


@pytest.mark.asyncio
async def test_passes_when_sentinel_absent(tmp_path):
    guard = KillSwitchGuard(str(tmp_path / "KILL"))
    result = await guard.run(Context(trace_id="t", event_id="e"))
    assert result.status == "success"


@pytest.mark.asyncio
async def test_halts_new_entries_when_sentinel_present(tmp_path):
    sentinel = tmp_path / "KILL"
    sentinel.write_text("stop")
    guard = KillSwitchGuard(str(sentinel))
    result = await guard.run(Context(trace_id="t", event_id="e"))
    assert result.status == "skip"
    assert result.reason == "kill_switch_engaged"
