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
    gw.get_option_ask = AsyncMock(return_value=(5.00, 0.0))  # live ask, age_s
    gw.cancel_order = AsyncMock(return_value=True)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="opt-1", perm_id=2,
        submitted_qty=20, filled_qty=20, remaining_qty=0, avg_fill_price=5.10,
        last_status="Filled", status_timestamp="2026-05-05T13:31:00Z",
    ))
    intent_store = MagicMock()
    intent_store.write = AsyncMock()
    return gw, intent_store


def _make_submitter(gw, intent_store, cap=0.05):
    return OptionsMarketSubmitter(
        gw, intent_store, fill_timeout=5.0, slippage_cap_pct=cap)


@pytest.mark.asyncio
async def test_writes_options_intent_with_parent_link(deps):
    gw, intent_store = deps
    sub = _make_submitter(gw, intent_store)
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
    # Marketable LMT off the LIVE ask (5.00) + 5% cap -> ceil(5.25) = 5.25.
    assert placed.order_type == "LMT"
    assert placed.limit_price == 5.25
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
    sub = _make_submitter(gw, intent_store)
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
async def test_falls_back_to_cached_ask_when_live_ask_unavailable(deps):
    # Live ask comes back 0 (no quote under delayed data); submitter must price
    # the limit off the cached sizing ask stashed in ctx as option_ask.
    gw, intent_store = deps
    gw.get_option_ask = AsyncMock(return_value=(0.0, 0.0))
    sub = _make_submitter(gw, intent_store)
    contract = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                                 currency="USD", strike=180.0, expiry="20261218",
                                 right="C", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "shares_intent_id": "shares-intent-1", "channel": "mystic",
        "ticker": "AAPL", "side": "long", "quantity": 20, "bucket": "HIGH",
        "selected_contract": contract, "selected_strike": 180.0,
        "selected_expiry": "2026-12-18", "received_at": "2026-05-05T13:30:00Z",
        "option_ask": 4.00,  # cached from OrderSizer
    })
    result = await sub.run(ctx)
    assert result.status == "success"
    placed = gw.place_order.call_args[0][1]
    # 4.00 cached ask + 5% -> ceil(4.20) = 4.20.
    assert placed.limit_price == 4.20


@pytest.mark.asyncio
async def test_no_ask_anywhere_partials_the_leg(deps):
    # Neither live nor cached ask available -> cannot price a limit -> partial
    # (shares already filled) and no order placed.
    gw, intent_store = deps
    gw.get_option_ask = AsyncMock(return_value=(0.0, 0.0))
    sub = _make_submitter(gw, intent_store)
    contract = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                                 currency="USD", strike=180.0, qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "shares_intent_id": "x", "ticker": "AAPL", "side": "long",
        "quantity": 10, "selected_contract": contract,
    })
    result = await sub.run(ctx)
    assert result.status == "success"
    assert "option_no_ask" in result.updates["partial_execution_reason"]
    gw.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_partial_records_real_qty_and_cancels_residual(deps):
    gw, intent_store = deps
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.TIMED_OUT_PENDING, broker_order_id="opt-1", perm_id=2,
        submitted_qty=20, filled_qty=8, remaining_qty=12, avg_fill_price=5.05,
        last_status="Submitted", status_timestamp="2026-05-05T13:31:00Z",
    ))
    sub = _make_submitter(gw, intent_store)
    contract = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                                 currency="USD", strike=180.0, expiry="20261218",
                                 right="C", qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"shares_intent_id": "s1", "channel": "mystic", "ticker": "AAPL",
                "side": "long", "quantity": 20, "bucket": "HIGH",
                "selected_contract": contract, "selected_strike": 180.0,
                "selected_expiry": "2026-12-18", "received_at": "2026-05-05T13:30:00Z"})
    result = await sub.run(ctx)
    assert result.status == "success"
    assert intent_store.write.call_args.kwargs["fill_qty"] == 8
    gw.cancel_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_fill_options_cancels_and_partials(deps):
    gw, intent_store = deps
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.TIMED_OUT_PENDING, broker_order_id="opt-1", perm_id=2,
        submitted_qty=20, filled_qty=0, remaining_qty=20, avg_fill_price=None,
        last_status="Submitted", status_timestamp="2026-05-05T13:31:00Z",
    ))
    sub = _make_submitter(gw, intent_store)
    contract = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                                 currency="USD", strike=180.0, qualified=True)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"shares_intent_id": "s1", "ticker": "AAPL", "side": "long",
                "quantity": 20, "selected_contract": contract})
    result = await sub.run(ctx)
    assert result.status == "success"  # partial_or: shares filled -> partial-success
    assert "options_not_filled" in result.updates["partial_execution_reason"]
    gw.cancel_order.assert_awaited_once()
    intent_store.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_signal_partial_when_shares_filled(deps):
    # With shares_intent_id set, a short-signal short-circuit converts to
    # partial-success rather than skip so the trace closes as success.
    gw, intent_store = deps
    sub = _make_submitter(gw, intent_store)
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
