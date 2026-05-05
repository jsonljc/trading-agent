import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock
from skills.execution.options_market_submitter import OptionsMarketSubmitter
from agent.context import Context
from infra.ib.models import FillResult, FillStatus, BrokerContractRef


@pytest.fixture
def deps():
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="opt-1", perm_id=2,
        submitted_qty=20, filled_qty=20, remaining_qty=0, avg_fill_price=5.10,
        last_status="Filled", status_timestamp="2026-05-05T13:31:00Z",
    ))
    intent_store = MagicMock()
    intent_store.write = AsyncMock()
    return gw, intent_store


@pytest.mark.asyncio
async def test_writes_options_intent_with_parent_link(deps):
    gw, intent_store = deps
    sub = OptionsMarketSubmitter(gw, intent_store, fill_timeout=5.0)
    contract = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                                 currency="USD", strike=180.0, expiry="20261218",
                                 right="C", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "shares_intent_id": "shares-intent-1",
        "channel": "mystic",
        "ticker": "AAPL", "side": "long", "quantity": 20,
        "bucket": "HIGH",
        "selected_contract": contract,
        "selected_strike": 180.0, "selected_expiry": "2026-12-18",
        "signal_received_at": "2026-05-05T13:30:00Z",
    })
    result = await sub.run(ctx)
    if result.updates:
        ctx.update(result.updates)
    assert result.status == "success"
    write_kwargs = intent_store.write.call_args.kwargs
    assert write_kwargs["instrument_type"] == "option"
    assert write_kwargs["parent_intent_id"] == "shares-intent-1"
    # Regression: the intent_store.write call must NOT pass None for
    # signal_received_at. The schema declares the column NOT NULL, so a None
    # would raise IntegrityError on insert.
    assert write_kwargs["signal_received_at"] is not None
    placed = gw.place_order.call_args[0][1]
    assert placed.order_type == "MKT"
    # Regression: client_order_id must carry real trace_id/event_id.
    client_order_id = gw.place_order.call_args[0][2]
    assert "t1" in client_order_id and "e1" in client_order_id
    assert "None" not in client_order_id


@pytest.mark.asyncio
async def test_falls_back_to_received_at_when_signal_received_at_absent(deps):
    # Regression: live ctx never sets signal_received_at; the chain instead
    # carries received_at (from the trigger event). Submitter must read
    # received_at, not signal_received_at.
    gw, intent_store = deps
    sub = OptionsMarketSubmitter(gw, intent_store, fill_timeout=5.0)
    contract = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                                 currency="USD", strike=180.0, expiry="20261218",
                                 right="C", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "shares_intent_id": "shares-intent-1",
        "channel": "mystic",
        "ticker": "AAPL", "side": "long", "quantity": 20,
        "bucket": "HIGH",
        "selected_contract": contract,
        "selected_strike": 180.0, "selected_expiry": "2026-12-18",
        "received_at": "2026-05-05T13:30:00Z",
        # signal_received_at intentionally absent
    })
    result = await sub.run(ctx)
    assert result.status == "success"
    write_kwargs = intent_store.write.call_args.kwargs
    assert write_kwargs["signal_received_at"] == "2026-05-05T13:30:00Z"


@pytest.mark.asyncio
async def test_short_signal_partial_when_shares_filled(deps):
    # With shares_intent_id set, a short-signal short-circuit converts to
    # partial-success rather than skip so the trace closes as success.
    gw, intent_store = deps
    sub = OptionsMarketSubmitter(gw, intent_store, fill_timeout=5.0)
    contract = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                                 currency="USD", strike=180.0, qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "shares_intent_id": "x", "ticker": "AAPL", "side": "short",
        "quantity": 10, "selected_contract": contract,
    })
    result = await sub.run(ctx)
    assert result.status == "success"
    assert result.updates["partial_execution_reason"] == "unsupported_short_signal"
    gw.place_order.assert_not_awaited()
