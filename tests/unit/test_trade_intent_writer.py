import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.trade_intent_writer import TradeIntentWriter


def _store():
    s = MagicMock()
    s.insert = AsyncMock()
    return s


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
