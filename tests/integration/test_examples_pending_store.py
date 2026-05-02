import pytest
from infra.storage.examples_pending_store import ExamplesPendingStore


@pytest.mark.asyncio
async def test_insert_and_list_pending(db):
    store = ExamplesPendingStore(db)
    await store.insert(trader_handle="wse", msg_text="x", proposed_bucket="LOW",
                       proposed_why="why", source="low_confidence")
    rows = await store.list_pending(trader_handle="wse")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_marks_status_and_bucket(db):
    store = ExamplesPendingStore(db)
    await store.insert(trader_handle="wse", msg_text="x", proposed_bucket="LOW",
                       proposed_why="w", source="low_confidence")
    pending = await store.list_pending(trader_handle="wse")
    pid = pending[0]["id"]
    await store.resolve(pid, status="approved", resolved_bucket="HIGH")
    remaining = await store.list_pending(trader_handle="wse")
    assert remaining == []
    resolved = await store.list_resolved(trader_handle="wse")
    assert resolved[0]["resolved_bucket"] == "HIGH"
    assert resolved[0]["status"] == "approved"
