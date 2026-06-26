import pytest
import aiosqlite
from infra.storage.db import SCHEMA
from infra.storage.trim_ladder_store import TrimLadderStore


@pytest.fixture
async def store():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    yield TrimLadderStore(conn)
    await conn.close()


@pytest.mark.asyncio
async def test_arm_inserts_two_rungs(store):
    await store.arm("intent-1", rungs=[(1, 0.05, 0.40), (2, 0.10, 0.40)],
                    armed_at="2026-05-05T10:00:00Z")
    rows = await store.unfired_for_intent("intent-1")
    assert len(rows) == 2
    assert {r["rung"] for r in rows} == {1, 2}


@pytest.mark.asyncio
async def test_record_fire_marks_rung_fired(store):
    await store.arm("intent-1", rungs=[(1, 0.05, 0.40)], armed_at="2026-05-05T10:00:00Z")
    await store.record_fire(
        intent_id="intent-1", rung=1,
        fired_at="2026-05-05T10:30:00Z",
        fire_price=110.0, sold_qty=4, sold_avg_price=110.05,
        broker_order_ref="order-99",
    )
    rows = await store.unfired_for_intent("intent-1")
    assert rows == []
    fired = await store.all_for_intent("intent-1")
    assert fired[0]["fired_at"] == "2026-05-05T10:30:00Z"
    assert fired[0]["sold_qty"] == 4


@pytest.mark.asyncio
async def test_unfired_across_intents(store):
    await store.arm("intent-1", rungs=[(1, 0.05, 0.40)], armed_at="t1")
    await store.arm("intent-2", rungs=[(1, 0.05, 0.40), (2, 0.10, 0.40)], armed_at="t2")
    rows = await store.all_unfired()
    intent_ids = {r["intent_id"] for r in rows}
    assert intent_ids == {"intent-1", "intent-2"}
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_claim_for_fire_rejects_double_claim(store):
    """INV-3 double-fire guard: claim_for_fire is atomic and idempotent-resistant.

    Verifies the full gate lifecycle on one armed rung:
      1. first claim  → True  (rung reserved in-flight)
      2. second claim → False  (in-flight rung cannot be double-claimed)
      3. after release_claim → True  (rung available again)
      4. after record_fire  → False  (a fired rung is never re-claimable)
    """
    await store.arm("intent-1", rungs=[(1, 0.05, 0.40)], armed_at="2026-05-05T10:00:00Z")
    ts = "2026-05-05T10:30:00Z"

    # 1. first claim succeeds
    assert await store.claim_for_fire("intent-1", 1, ts) is True

    # 2. second claim on the in-flight rung is rejected
    assert await store.claim_for_fire("intent-1", 1, ts) is False

    # 3. release the in-flight claim; next claim succeeds again
    await store.release_claim("intent-1", 1)
    assert await store.claim_for_fire("intent-1", 1, ts) is True

    # 4. record the fire (sets fired_at); rung is permanently non-claimable
    await store.record_fire(
        intent_id="intent-1", rung=1,
        fired_at="2026-05-05T10:31:00Z",
        fire_price=110.0, sold_qty=4, sold_avg_price=110.05,
        broker_order_ref="order-99",
    )
    assert await store.claim_for_fire("intent-1", 1, ts) is False
