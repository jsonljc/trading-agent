import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.trade_intent_writer import TradeIntentWriter


def _store():
    s = MagicMock()
    s.insert = AsyncMock()
    return s


class _FakeIntentStore:
    """Minimal fake store that records the last inserted row."""
    def __init__(self):
        self.last_written: dict | None = None

    async def insert(self, record: dict) -> None:
        self.last_written = record


@pytest.fixture
def fake_intent_store():
    return _FakeIntentStore()


def _ctx(event_id="evt1", channel="mystic", ticker="NVDA",
         intent="LONG_SIGNAL", bucket="HIGH",
         received_at="2026-04-24T10:00:00+00:00"):
    ctx = Context(trace_id="t1", event_id=event_id)
    ctx.update({
        "channel": channel,
        "ticker": ticker,
        "intent": intent,
        "bucket": bucket,
        "received_at": received_at,
    })
    return ctx


async def test_creates_intent_row_and_sets_intent_id():
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = _ctx()
    result = await skill.run(ctx)
    assert result.status == "success"
    assert ctx.get("intent_id") == "evt1:NVDA:long"
    store.insert.assert_called_once()
    record = store.insert.call_args[0][0]
    assert record["ticker"] == "NVDA"
    assert record["side"] == "long"
    assert record["conviction"] == "HIGH"
    assert record["channel"] == "mystic"
    assert record["policy_state"] == "approved"
    assert record["execution_state"] is None


async def test_add_signal_maps_to_long():
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = _ctx(intent="ADD_SIGNAL")
    await skill.run(ctx)
    record = store.insert.call_args[0][0]
    assert record["side"] == "long"


async def test_uses_side_key_if_set_by_signal_analyzer():
    """TraderClassifier sets 'side' directly; TradeIntentWriter prefers it."""
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = _ctx()
    ctx.update({"side": "short"})
    await skill.run(ctx)
    record = store.insert.call_args[0][0]
    assert record["side"] == "short"
    assert ctx.get("intent_id") == "evt1:NVDA:short"


async def test_missing_ticker_returns_fail():
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": "mystic", "intent": "LONG_SIGNAL", "bucket": "HIGH",
                "received_at": "2026-04-24T10:00:00+00:00"})
    result = await skill.run(ctx)
    assert result.status == "fail"
    store.insert.assert_not_called()


async def test_unknown_intent_fails_rather_than_silently_defaulting_to_long():
    """If an intent we don't recognise reaches TradeIntentWriter, we must
    fail rather than silently coerce to 'long'. Otherwise a future
    SHORT_SIGNAL or unhandled intent value writes a long trade against
    the wrong direction."""
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = _ctx(intent="SHORT_SIGNAL")
    result = await skill.run(ctx)
    assert result.status == "fail", f"unknown intent must fail, got {result.status}"
    assert "intent" in (result.reason or "").lower()
    store.insert.assert_not_called()


@pytest.mark.asyncio
async def test_writes_equity_when_ctx_says_equity(fake_intent_store):
    """When ctx['instrument_type']='equity', the row written has instrument_type='equity'."""
    writer = TradeIntentWriter(fake_intent_store)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "channel": "mystic", "ticker": "AAPL",
        "side": "long", "bucket": "HIGH",
        "instrument_type": "equity",
    })
    result = await writer.run(ctx)
    if result.updates:
        ctx.update(result.updates)
    row = fake_intent_store.last_written
    assert row["instrument_type"] == "equity"
    assert row.get("parent_intent_id") is None


@pytest.mark.asyncio
async def test_writes_option_with_parent_intent_id(fake_intent_store):
    """When ctx['instrument_type']='option' and parent_intent_id is set, both flow through."""
    writer = TradeIntentWriter(fake_intent_store)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "channel": "mystic", "ticker": "AAPL",
        "side": "long", "bucket": "HIGH",
        "instrument_type": "option",
        "parent_intent_id": "shares-intent-123",
    })
    await writer.run(ctx)
    row = fake_intent_store.last_written
    assert row["instrument_type"] == "option"
    assert row["parent_intent_id"] == "shares-intent-123"
