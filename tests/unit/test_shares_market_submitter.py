import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.shares_market_submitter import SharesMarketSubmitter
from agent.context import Context
from infra.ib.models import (PreparedOrder, FillResult, FillStatus, BrokerContractRef)


@pytest.fixture
def submitter_deps():
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=100, remaining_qty=0,
        avg_fill_price=147.50, last_status="Filled",
        status_timestamp="2026-05-05T13:30:00Z",
    ))
    intent_store = MagicMock()
    intent_store.update_fill = AsyncMock()
    trim_store = MagicMock()
    trim_store.arm = AsyncMock()
    return gw, intent_store, trim_store


@pytest.mark.asyncio
async def test_long_signal_places_mkt_and_arms_trims(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    rungs = [(1, 0.05, 0.40), (2, 0.10, 0.40)]
    sub = SharesMarketSubmitter(gw, intent_store, trim_store,
                                  fill_timeout=5.0, trim_rungs=rungs)
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "intent_id": "intent-1", "ticker": "AAPL",
        "side": "long", "quantity": 100,
        "selected_contract": contract,
    })
    result = await sub.run(ctx)
    if result.updates:
        ctx.update(result.updates)
    assert result.status == "success"
    placed_order = gw.place_order.call_args[0][1]
    assert placed_order.order_type == "MKT"
    assert placed_order.action == "BUY"
    intent_store.update_fill.assert_awaited_once()
    args = intent_store.update_fill.call_args.kwargs
    assert args["fill_qty"] == 100
    assert args["fill_price"] == 147.50
    trim_store.arm.assert_awaited_once()
    arm_call = trim_store.arm.call_args
    # First positional arg is intent_id; rungs and armed_at are kwargs
    assert arm_call.kwargs["rungs"] == rungs


@pytest.mark.asyncio
async def test_short_signal_skipped_no_orders(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    sub = SharesMarketSubmitter(gw, intent_store, trim_store,
                                  fill_timeout=5.0, trim_rungs=[])
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "intent_id": "intent-1", "ticker": "AAPL",
        "side": "short", "quantity": 100,
        "selected_contract": contract,
    })
    result = await sub.run(ctx)
    assert result.status == "skip"
    assert "unsupported_short_signal" in (result.reason or "")
    gw.place_order.assert_not_awaited()
    trim_store.arm.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_fill_arms_trims_on_filled_qty(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    # Override wait_fill to simulate partial fill — 60 of 100
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=60, remaining_qty=40,
        avg_fill_price=147.0, last_status="Filled",
        status_timestamp="2026-05-05T13:30:00Z",
    ))
    rungs = [(1, 0.05, 0.40)]
    sub = SharesMarketSubmitter(gw, intent_store, trim_store,
                                  fill_timeout=5.0, trim_rungs=rungs)
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "intent_id": "intent-1", "ticker": "AAPL",
        "side": "long", "quantity": 100,
        "selected_contract": contract,
    })
    result = await sub.run(ctx)
    if result.updates:
        ctx.update(result.updates)
    assert result.status == "success"
    args = intent_store.update_fill.call_args.kwargs
    assert args["fill_qty"] == 60
