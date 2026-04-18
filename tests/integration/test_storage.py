import pytest
import aiosqlite
from infra.storage.db import SCHEMA
from infra.storage.idempotency_store import IdempotencyStore
from infra.storage.trace_store import TraceStore
from infra.storage.signal_store import SignalStore


@pytest.fixture
async def conn():
    async with aiosqlite.connect(":memory:") as c:
        c.row_factory = aiosqlite.Row
        await c.executescript(SCHEMA)
        await c.commit()
        yield c


async def test_idempotency_insert_and_check(conn):
    store = IdempotencyStore(conn)
    inserted = await store.insert_if_new("key1", "evt1", "AVEX", "long")
    assert inserted is True
    duplicate = await store.insert_if_new("key1", "evt1", "AVEX", "long")
    assert duplicate is False


async def test_idempotency_duplicate_insert_is_ignored(conn):
    store = IdempotencyStore(conn)
    await store.insert_if_new("key1", "evt1", "AVEX", "long")
    await store.insert_if_new("key1", "evt1", "AVEX", "long")  # should not raise


async def test_trace_start_and_finish(conn):
    store = TraceStore(conn)
    await store.start("trace1", "evt1")
    await store.finish("trace1", "success")
    async with conn.execute("SELECT status FROM work_traces WHERE trace_id='trace1'") as cur:
        row = await cur.fetchone()
    assert row["status"] == "success"


async def test_signal_insert(conn):
    store = SignalStore(conn)
    await store.insert({
        "id": "evt1", "source": "discord_notification",
        "channel": "mystic", "author": "UndefinedMystic",
        "trigger_preview": "Long $AVEX", "full_message_text": "Long $AVEX today's IPO",
        "capture_mode": "preview", "message_fingerprint": "abc123",
    })
    async with conn.execute("SELECT channel FROM signal_events WHERE id='evt1'") as cur:
        row = await cur.fetchone()
    assert row["channel"] == "mystic"


async def test_trace_record_skill_writes_json(conn):
    store = TraceStore(conn)
    await store.start("trace2", "evt2")
    await store.record_skill("trace2", "message_normalizer", "success", {"fingerprint": "abc"})
    async with conn.execute(
        "SELECT skill_name, output_json FROM skill_outputs WHERE trace_id='trace2'"
    ) as cur:
        row = await cur.fetchone()
    assert row["skill_name"] == "message_normalizer"
    import json
    assert json.loads(row["output_json"])["fingerprint"] == "abc"


async def test_trace_finish_with_failure_reason(conn):
    store = TraceStore(conn)
    await store.start("trace3", "evt3")
    await store.finish("trace3", "failed", "ticker ambiguous")
    async with conn.execute(
        "SELECT status, failure_reason FROM work_traces WHERE trace_id='trace3'"
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"
    assert row["failure_reason"] == "ticker ambiguous"


async def test_idempotency_insert_if_new(conn):
    store = IdempotencyStore(conn)
    inserted = await store.insert_if_new("key2", "evt2", "TSLA", "long")
    assert inserted is True
    duplicate = await store.insert_if_new("key2", "evt2", "TSLA", "long")
    assert duplicate is False
