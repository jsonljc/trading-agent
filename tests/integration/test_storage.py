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
    assert not await store.exists("key1")
    await store.insert("key1", "evt1", "AVEX", "long")
    assert await store.exists("key1")


async def test_idempotency_duplicate_insert_is_ignored(conn):
    store = IdempotencyStore(conn)
    await store.insert("key1", "evt1", "AVEX", "long")
    await store.insert("key1", "evt1", "AVEX", "long")  # should not raise
    assert await store.exists("key1")


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
