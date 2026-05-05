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


@pytest.mark.asyncio
async def test_trade_intents_fill_qty_and_parent_intent_id_nullable():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(trade_intents)") as cur:
            rows = {r["name"]: r for r in await cur.fetchall()}
        assert rows["fill_qty"]["notnull"] == 0
        assert rows["parent_intent_id"]["notnull"] == 0


@pytest.mark.asyncio
async def test_trade_intent_trims_composite_pk_enforced():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        # First insert succeeds
        await conn.execute(
            "INSERT INTO trade_intent_trims (intent_id, rung, threshold_pct, trim_pct, armed_at) "
            "VALUES (?,?,?,?,?)",
            ("intent-x", 1, 0.05, 0.40, "2026-05-05T10:00:00Z"),
        )
        await conn.commit()
        # Second insert with same (intent_id, rung) must fail
        with pytest.raises(Exception):
            await conn.execute(
                "INSERT INTO trade_intent_trims (intent_id, rung, threshold_pct, trim_pct, armed_at) "
                "VALUES (?,?,?,?,?)",
                ("intent-x", 1, 0.10, 0.40, "2026-05-05T10:01:00Z"),
            )
            await conn.commit()


@pytest.mark.asyncio
async def test_trade_intent_trims_fk_enforced():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(Exception):
            await conn.execute(
                "INSERT INTO trade_intent_trims (intent_id, rung, threshold_pct, trim_pct, armed_at) "
                "VALUES (?,?,?,?,?)",
                ("nonexistent-intent", 1, 0.05, 0.40, "2026-05-05T10:00:00Z"),
            )
            await conn.commit()


@pytest.mark.asyncio
async def test_trade_intent_trims_partial_index_predicate():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_trade_intent_trims_unfired'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, "partial index missing"
        # Normalize whitespace to handle any SQLite version differences
        sql_normalized = " ".join(row[0].split())
        assert "fired_at IS NULL" in sql_normalized
