import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.shares_market_submitter import SharesMarketSubmitter
from agent.context import Context
from infra.ib.models import (PreparedOrder, FillResult, FillStatus, BrokerContractRef)


@pytest.fixture
def submitter_deps():
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.get_quote = AsyncMock(return_value=150.0)  # live equity ask
    gw.cancel_order = AsyncMock(return_value=True)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=100, remaining_qty=0,
        avg_fill_price=147.50, last_status="Filled",
        status_timestamp="2026-05-05T13:30:00Z",
    ))
    intent_store = MagicMock()
    intent_store.update_fill = AsyncMock()
    intent_store.update_execution_state = AsyncMock()
    trim_store = MagicMock()
    trim_store.arm = AsyncMock()
    return gw, intent_store, trim_store


def _make_submitter(gw, intent_store, trim_store, *, rungs, cap=0.01):
    return SharesMarketSubmitter(
        gw, intent_store, trim_store, fill_timeout=5.0,
        trim_rungs=rungs, slippage_cap_pct=cap,
    )


@pytest.mark.asyncio
async def test_long_signal_places_mkt_and_arms_trims(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    rungs = [(1, 0.05, 0.40), (2, 0.10, 0.40)]
    sub = _make_submitter(gw, intent_store, trim_store, rungs=rungs)
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
    # Marketable LMT priced at live ask (150.0) + 1% cap -> 151.50.
    assert placed_order.order_type == "LMT"
    assert placed_order.limit_price == 151.50
    assert placed_order.action == "BUY"
    # Regression: client_order_id must carry real trace_id/event_id (ctx
    # attribute access), not the empty string from ctx.get("trace_id") which
    # looked in the data dict.
    client_order_id = gw.place_order.call_args[0][2]
    assert "t1" in client_order_id
    assert "e1" in client_order_id
    assert "None" not in client_order_id
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
    sub = _make_submitter(gw, intent_store, trim_store, rungs=[])
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
    sub = _make_submitter(gw, intent_store, trim_store, rungs=rungs)
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


@pytest.mark.asyncio
async def test_timed_out_partial_cancels_residual_and_arms_trims(submitter_deps):
    # A genuine partial (TIMED_OUT_PENDING, 60 of 100 filled): record the real
    # fill, cancel the residual working order, arm trims on the filled qty, and
    # return success so the options leg still fires.
    gw, intent_store, trim_store = submitter_deps
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.TIMED_OUT_PENDING, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=60, remaining_qty=40,
        avg_fill_price=147.0, last_status="Submitted",
        status_timestamp="2026-05-05T13:30:00Z",
    ))
    rungs = [(1, 0.05, 0.40)]
    sub = _make_submitter(gw, intent_store, trim_store, rungs=rungs)
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"intent_id": "intent-1", "ticker": "AAPL",
                "side": "long", "quantity": 100, "selected_contract": contract})
    result = await sub.run(ctx)
    assert result.status == "success"
    assert intent_store.update_fill.call_args.kwargs["fill_qty"] == 60
    gw.cancel_order.assert_awaited_once()
    trim_store.arm.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_ahead_submitted_before_fill(submitter_deps):
    # Crash-recovery: the submitter must persist a 'submitted'/'dispatched'
    # write-ahead (with broker_order_ref) BEFORE waiting for the fill, so a
    # crash mid-fill leaves a reconcilable row.
    gw, intent_store, trim_store = submitter_deps
    sub = _make_submitter(gw, intent_store, trim_store, rungs=[])
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"intent_id": "intent-1", "ticker": "AAPL",
                "side": "long", "quantity": 100, "selected_contract": contract})
    await sub.run(ctx)
    wa = intent_store.update_execution_state.call_args_list[0]
    assert wa.args[1] == "submitted"
    assert wa.kwargs["outbox_status"] == "dispatched"
    # Stable client_order_id (orderRef), not the per-session orderId.
    assert ":shares:" in wa.kwargs["broker_order_ref"]
    assert wa.kwargs["order_submitted_at"] is not None


@pytest.mark.asyncio
async def test_rejected_order_marks_failed_dlq(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.REJECTED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=0, remaining_qty=100,
        avg_fill_price=None, last_status="Inactive",
        status_timestamp="2026-05-05T13:30:00Z",
    ))
    sub = _make_submitter(gw, intent_store, trim_store, rungs=[(1, 0.05, 0.40)])
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"intent_id": "intent-1", "ticker": "AAPL",
                "side": "long", "quantity": 100, "selected_contract": contract})
    result = await sub.run(ctx)
    assert result.status == "fail"
    assert "shares_rejected" in (result.reason or "")
    # Last state write marks the DLQ row.
    failed = intent_store.update_execution_state.call_args_list[-1]
    assert failed.args[1] == "failed"
    assert failed.kwargs["dlq_reason"] is not None
    assert failed.kwargs["outbox_status"] == "failed"
    trim_store.arm.assert_not_awaited()
    gw.cancel_order.assert_not_awaited()  # a rejected order has no residual


@pytest.mark.asyncio
async def test_zero_fill_timeout_cancels_residual_and_fails(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.TIMED_OUT_PENDING, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=0, remaining_qty=100,
        avg_fill_price=None, last_status="Submitted",
        status_timestamp="2026-05-05T13:30:00Z",
    ))
    sub = _make_submitter(gw, intent_store, trim_store, rungs=[(1, 0.05, 0.40)])
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"intent_id": "intent-1", "ticker": "AAPL",
                "side": "long", "quantity": 100, "selected_contract": contract})
    result = await sub.run(ctx)
    assert result.status == "fail"
    assert "shares_not_filled" in (result.reason or "")
    gw.cancel_order.assert_awaited_once()
    trim_store.arm.assert_not_awaited()
    intent_store.update_fill.assert_not_awaited()
    # Timeout is distinct from rejection: cancelled, not failed/DLQ.
    cancelled = intent_store.update_execution_state.call_args_list[-1]
    assert cancelled.args[1] == "cancelled"
    assert cancelled.kwargs["cancel_reason"] == "fill_timeout"
    assert cancelled.kwargs["outbox_status"] == "cancelled"
