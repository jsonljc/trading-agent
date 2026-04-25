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
