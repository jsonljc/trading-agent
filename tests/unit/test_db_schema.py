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
