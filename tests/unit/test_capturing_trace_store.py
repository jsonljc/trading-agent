import pytest

from agent.replay.capture import CapturingTraceStore


@pytest.mark.asyncio
async def test_records_path_updates_and_terminal_status():
    store = CapturingTraceStore()
    await store.start("t1", "e1")
    await store.record_skill("t1", "TraderRouter", "success", {"trader_handle": "x"})
    await store.record_skill("t1", "TraderClassifier", "success", {"bucket": "HIGH"})
    await store.finish("t1", "success")

    rec = store.records["t1"]
    assert rec["event_id"] == "e1"
    assert rec["path"] == [("TraderRouter", "success"), ("TraderClassifier", "success")]
    assert rec["updates"]["trader_handle"] == "x"
    assert rec["updates"]["bucket"] == "HIGH"
    assert rec["status"] == "success"
    assert rec["reason"] is None


@pytest.mark.asyncio
async def test_skip_records_reason():
    store = CapturingTraceStore()
    await store.start("t2", "e2")
    await store.record_skill("t2", "EntrySkipGate", "skip", {})
    await store.finish("t2", "skipped", "no_entry:bucket=SKIP")
    rec = store.records["t2"]
    assert rec["status"] == "skipped"
    assert rec["reason"] == "no_entry:bucket=SKIP"
    assert rec["path"][-1] == ("EntrySkipGate", "skip")
