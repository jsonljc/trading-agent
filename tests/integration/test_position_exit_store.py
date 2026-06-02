import pytest
from datetime import datetime, timezone
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from infra.storage.position_exit_store import PositionExitStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _intent(intent_id, *, channel="mystic", ticker="AAPL", instrument="equity",
            state="filled", fill_qty=100):
    now = _now()
    return {
        "intent_id": intent_id, "event_id": intent_id.split(":")[0],
        "channel": channel, "ticker": ticker, "side": "long",
        "instrument_type": instrument, "conviction": "HIGH",
        "policy_state": "approved", "execution_state": state, "fill_qty": fill_qty,
        "fill_price": 100.0, "filled_at": now,
        "signal_received_at": now, "intent_created_at": now,
        "created_at": now, "updated_at": now,
    }


async def test_get_open_shares_positions_filters_channel_ticker_state_instrument(db):
    store = TradeIntentStore(db)
    await store.insert(_intent("e1:AAPL:long"))                       # match
    await store.insert(_intent("e2:AAPL:long", instrument="option"))  # option -> excluded
    await store.insert(_intent("e3:AAPL:long", state="cancelled"))    # not filled -> excluded
    await store.insert(_intent("e4:AAPL:long", channel="wse"))        # other channel -> excluded
    await store.insert(_intent("e5:TSLA:long", ticker="TSLA"))       # other ticker -> excluded
    rows = await store.get_open_shares_positions("mystic", "AAPL")
    assert [r["intent_id"] for r in rows] == ["e1:AAPL:long"]


async def test_shares_intent_instrument_type_literal_is_equity(db):
    # Guards the get_open_shares_positions filter against a literal drift: the
    # entry path writes shares intents with instrument_type='equity'.
    from skills.execution.trade_intent_writer import TradeIntentWriter
    from agent.context import Context
    ctx = Context(trace_id="t", event_id="e")
    ctx.update({"ticker": "AAPL", "side": "long", "channel": "mystic", "bucket": "HIGH"})
    await TradeIntentWriter(TradeIntentStore(db)).run(ctx)
    row = await TradeIntentStore(db).get("e:AAPL:long")
    assert row["instrument_type"] == "equity"


async def test_claim_sell_event_is_idempotent(db):
    exits = PositionExitStore(db)
    assert await exits.claim_sell_event("fp-1", "evtA") is True
    # Same fingerprint (reposted/redelivered) -> not claimed again.
    assert await exits.claim_sell_event("fp-1", "evtB") is False


async def test_record_exit_and_sold_qty(db):
    exits = PositionExitStore(db)
    await exits.record_exit(
        fingerprint="fp", event_id="e", intent_id="e1:AAPL:long",
        channel="mystic", ticker="AAPL", scope="partial", requested_qty=40,
        sold_qty=40, sold_avg_price=99.5, broker_order_ref="IB-1", reason="trim")
    await exits.record_exit(
        fingerprint="fp2", event_id="e2", intent_id="e1:AAPL:long",
        channel="mystic", ticker="AAPL", scope="full", requested_qty=20,
        sold_qty=20, sold_avg_price=98.0, broker_order_ref="IB-2", reason="close")
    assert await exits.sold_qty_for_intent("e1:AAPL:long") == 60


async def test_remaining_qty_nets_trims_exits_and_reserves_inflight(db):
    intents = TradeIntentStore(db)
    trims = TrimLadderStore(db)
    exits = PositionExitStore(db)
    await intents.insert(_intent("e1:AAPL:long", fill_qty=100))

    # No trims/exits yet -> full position.
    assert await exits.remaining_qty("e1:AAPL:long") == 100

    await trims.arm("e1:AAPL:long", rungs=[(1, 0.05, 0.40), (2, 0.10, 0.40)],
                    armed_at=_now())
    assert await exits.remaining_qty("e1:AAPL:long") == 100  # armed but not fired

    # Claim rung 1 (in-flight, not yet recorded) -> reserve round(100*0.40)=40.
    await trims.claim_for_fire("e1:AAPL:long", 1, _now())
    assert await exits.remaining_qty("e1:AAPL:long") == 60

    # Record the fire (actual 40 sold) -> still 60, now from recorded not reserved.
    await trims.record_fire(intent_id="e1:AAPL:long", rung=1, fired_at=_now(),
                            fire_price=105.0, sold_qty=40, sold_avg_price=105.0,
                            broker_order_ref="t1")
    assert await exits.remaining_qty("e1:AAPL:long") == 60

    # A follow-sell exit of 30 -> remaining 30.
    await exits.record_exit(
        fingerprint="fp", event_id="e", intent_id="e1:AAPL:long",
        channel="mystic", ticker="AAPL", scope="partial", requested_qty=30,
        sold_qty=30, sold_avg_price=101.0, broker_order_ref="x", reason="trim")
    assert await exits.remaining_qty("e1:AAPL:long") == 30


async def test_migration_creates_exit_tables_on_legacy_db(tmp_path):
    # A DB created before Phase E (no exit tables) gains them via _migrate().
    import aiosqlite
    from infra.storage.db import get_connection
    legacy = str(tmp_path / "legacy.db")
    conn = await aiosqlite.connect(legacy)
    await conn.execute("CREATE TABLE trade_intents (intent_id TEXT)")
    await conn.commit()
    await conn.close()
    conn = await get_connection(legacy)
    try:
        for tbl in ("position_exits", "sell_event_claims"):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,)) as cur:
                assert await cur.fetchone() is not None, tbl
    finally:
        await conn.close()
