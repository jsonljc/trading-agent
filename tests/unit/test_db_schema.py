import pytest
import aiosqlite
from infra.storage.db import SCHEMA


@pytest.mark.asyncio
async def test_schema_creates_classification_log_and_pending_and_state():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(SCHEMA)
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('classification_log','trader_examples_pending','trader_state')"
        )
        rows = await cursor.fetchall()
        names = {r[0] for r in rows}
    assert names == {"classification_log", "trader_examples_pending", "trader_state"}


@pytest.mark.asyncio
async def test_schema_creates_idx_classification_log_trader_time():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(SCHEMA)
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_classification_log_trader_time'"
        )
        row = await cursor.fetchone()
    assert row is not None, "index idx_classification_log_trader_time must exist after SCHEMA is applied"


@pytest.mark.asyncio
async def test_busy_timeout_is_set_explicitly(tmp_path):
    # Python's sqlite connect() defaults busy_timeout to 5000ms; we set it
    # explicitly to 10000ms for extra headroom when a second connection (an
    # audit/promote script) touches agent.db during a live trade.
    from infra.storage.db import get_connection
    conn = await get_connection(str(tmp_path / "t.db"))
    try:
        async with conn.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
        assert row[0] == 10000
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_check_integrity_ok(tmp_path):
    from infra.storage.db import get_connection, check_integrity
    conn = await get_connection(str(tmp_path / "t.db"))
    try:
        assert await check_integrity(conn) == "ok"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_backup_database_creates_usable_snapshot(tmp_path):
    from infra.storage.db import get_connection, backup_database
    conn = await get_connection(str(tmp_path / "src.db"))
    try:
        await conn.execute(
            "INSERT INTO signal_events (id, source) VALUES ('x', 'discord')")
        await conn.commit()
        dest = str(tmp_path / "backup" / "snap.db")
        await backup_database(conn, dest)
    finally:
        await conn.close()
    import os
    assert os.path.exists(dest)
    # The snapshot is a real, queryable sqlite db carrying the row.
    snap = await aiosqlite.connect(dest)
    try:
        async with snap.execute("SELECT source FROM signal_events WHERE id='x'") as cur:
            row = await cur.fetchone()
        assert row[0] == "discord"
    finally:
        await snap.close()
