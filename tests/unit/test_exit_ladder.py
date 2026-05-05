import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.exit_ladder import fire_rung_if_crossed
from infra.ib.models import FillResult, FillStatus, BrokerContractRef


@pytest.mark.asyncio
async def test_does_not_fire_below_threshold():
    gw = MagicMock()
    intents = MagicMock()
    trims = MagicMock()
    trims.record_fire = AsyncMock()
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=104.0,
    )
    assert fired is False
    trims.record_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_fires_at_threshold_and_records():
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=contract)
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="sell-1", perm_id=9,
        submitted_qty=40, filled_qty=40, remaining_qty=0, avg_fill_price=105.10,
        last_status="Filled", status_timestamp="2026-05-05T14:00:00Z",
    ))
    trims = MagicMock()
    trims.record_fire = AsyncMock()
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=105.0,
    )
    assert fired is True
    placed = gw.place_order.call_args[0][1]
    assert placed.order_type == "MKT"
    assert placed.action == "SELL"
    assert placed.quantity == 40
    trims.record_fire.assert_awaited_once()


@pytest.mark.asyncio
async def test_rounds_trim_qty_minimum_one():
    """trim_pct=0.40 on original_qty=2 → round(0.8) = 1 (min 1 share)."""
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=contract)
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="sell-2", perm_id=10,
        submitted_qty=1, filled_qty=1, remaining_qty=0, avg_fill_price=105.0,
        last_status="Filled", status_timestamp="2026-05-05T14:00:00Z",
    ))
    trims = MagicMock()
    trims.record_fire = AsyncMock()
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=2, rung=1,
        threshold_pct=0.05, trim_pct=0.40, current_price=105.0,
    )
    assert fired is True
    placed = gw.place_order.call_args[0][1]
    assert placed.quantity >= 1
