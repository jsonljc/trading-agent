import pytest
import json
from infra.storage.classification_log_store import ClassificationLogStore


@pytest.mark.asyncio
async def test_insert_and_read_back(db):
    store = ClassificationLogStore(db)
    await store.insert(
        event_id="evt1", trader_handle="wse",
        msg_text="Added 2% AUDC", features={"x": 1},
        llm_response=None, bucket="LOW", confidence=1.0,
        size_pct=0.02, size_source="shortcut_stated",
        action_taken="fired", reason="stated_size_in_message",
    )
    rows = await store.recent_for_trader("wse", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "evt1"
    assert row["bucket"] == "LOW"
    assert row["size_pct"] == 0.02
    assert json.loads(row["features_json"]) == {"x": 1}
    assert row["llm_response_json"] is None


@pytest.mark.asyncio
async def test_recent_returns_newest_first(db):
    store = ClassificationLogStore(db)
    for i in range(3):
        await store.insert(
            event_id=f"e{i}", trader_handle="wse", msg_text=f"m{i}",
            features={}, llm_response=None, bucket="SKIP",
            confidence=0.9, size_pct=0.0, size_source="skip",
            action_taken="skipped", reason="x",
        )
    rows = await store.recent_for_trader("wse", limit=2)
    assert [r["event_id"] for r in rows] == ["e2", "e1"]
