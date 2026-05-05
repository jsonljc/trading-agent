import pytest
import aiosqlite
from infra.storage.db import SCHEMA


@pytest.mark.asyncio
async def test_trade_intents_has_fill_qty_and_parent_intent_id():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(trade_intents)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "fill_qty" in cols
        assert "parent_intent_id" in cols


@pytest.mark.asyncio
async def test_trade_intent_trims_table_exists():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(trade_intent_trims)") as cur:
            cols = {row["name"]: row for row in await cur.fetchall()}
        for col in ("intent_id", "rung", "threshold_pct", "trim_pct",
                    "armed_at", "fired_at", "fire_price",
                    "sold_qty", "sold_avg_price", "broker_order_ref"):
            assert col in cols, f"trade_intent_trims missing {col}"
