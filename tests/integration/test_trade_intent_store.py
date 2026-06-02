import pytest
from datetime import datetime, timezone
from infra.storage.trade_intent_store import TradeIntentStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _base_intent(intent_id="evt1:NVDA:long"):
    now = _now()
    return {
        "intent_id": intent_id,
        "event_id": "evt1",
        "channel": "mystic",
        "ticker": "NVDA",
        "side": "long",
        "instrument_type": "option",
        "conviction": "high",
        "policy_state": "approved",
        "signal_received_at": now,
        "intent_created_at": now,
        "created_at": now,
        "updated_at": now,
    }


async def test_insert_and_get(db):
    store = TradeIntentStore(db)
    intent = _base_intent()
    await store.insert(intent)
    row = await store.get("evt1:NVDA:long")
    assert row["ticker"] == "NVDA"
    assert row["policy_state"] == "approved"


async def test_update_policy_state(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    await store.update_policy_state("evt1:NVDA:long", "channel_blocked")
    row = await store.get("evt1:NVDA:long")
    assert row["policy_state"] == "channel_blocked"


async def test_update_execution_state(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    now = _now()
    await store.update_execution_state(
        "evt1:NVDA:long",
        execution_state="filled",
        fill_price=5.25,
        filled_at=now,
        outbox_status="confirmed",
    )
    row = await store.get("evt1:NVDA:long")
    assert row["execution_state"] == "filled"
    assert row["fill_price"] == pytest.approx(5.25)
    assert row["outbox_status"] == "confirmed"


async def test_update_outbox_status(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    await store.update_outbox_status("evt1:NVDA:long", "pending")
    row = await store.get("evt1:NVDA:long")
    assert row["outbox_status"] == "pending"


async def test_get_filled_since(db):
    store = TradeIntentStore(db)
    now = _now()
    filled_intent = {**_base_intent("evt2:NVDA:long"), "event_id": "evt2"}
    await store.insert(filled_intent)
    await store.update_execution_state(
        "evt2:NVDA:long",
        execution_state="filled",
        filled_at=now,
        fill_price=5.0,
        outbox_status="confirmed",
    )
    rows = await store.get_filled_since("NVDA", "2020-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "NVDA"


async def test_get_pending_outbox(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    await store.update_outbox_status("evt1:NVDA:long", "pending")
    rows = await store.get_pending_outbox()
    assert len(rows) == 1
    assert rows[0]["outbox_status"] == "pending"


async def test_update_fill_sets_filled_at_and_broker_ref(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent("evt9:NVDA:long"))
    await store.update_fill(
        "evt9:NVDA:long", fill_price=12.5, fill_qty=10,
        broker_order_ref="IB-123",
    )
    row = await store.get("evt9:NVDA:long")
    assert row["execution_state"] == "filled"
    assert row["fill_price"] == pytest.approx(12.5)
    assert row["fill_qty"] == 10
    assert row["broker_order_ref"] == "IB-123"
    assert row["filled_at"] is not None
    # Cooldown revival: get_filled_since now finds the fill.
    rows = await store.get_filled_since("NVDA", "2020-01-01T00:00:00+00:00")
    assert len(rows) == 1
    # A confirmed fill advances the outbox out of the in-flight set.
    assert row["outbox_status"] == "confirmed"
    assert len(await store.get_pending_outbox()) == 0


async def test_write_records_filled_at_and_broker_ref(db):
    store = TradeIntentStore(db)
    now = _now()
    await store.write(
        intent_id="evt9:NVDA:option", event_id="evt9", channel="mystic",
        ticker="NVDA", side="long", instrument_type="option",
        parent_intent_id="evt9:NVDA:long", expiry="2027-01-15", strike=150.0,
        right="C", conviction="HIGH", fill_price=3.2, fill_qty=2,
        execution_state="filled", signal_received_at=now,
        broker_order_ref="IB-456",
    )
    row = await store.get("evt9:NVDA:option")
    assert row["broker_order_ref"] == "IB-456"
    assert row["filled_at"] is not None
