import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock
from agent.exit_ladder import fire_rung_if_crossed, _in_rth
from infra.ib.models import FillResult, FillStatus, BrokerContractRef


@pytest.mark.asyncio
async def test_does_not_fire_below_threshold():
    gw = MagicMock()
    intents = MagicMock()
    trims = MagicMock()
    trims.record_fire = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=10**9)
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits,
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
    trims.claim_for_fire = AsyncMock(return_value=True)
    trims.release_claim = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=10**9)
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=105.0,
    )
    assert fired is True
    placed = gw.place_order.call_args[0][1]
    # Marketable SELL limit at current_price(105) * (1 - 1% default) -> 103.95.
    assert placed.order_type == "LMT"
    assert placed.limit_price == 103.95
    assert placed.action == "SELL"
    assert placed.quantity == 40
    trims.record_fire.assert_awaited_once()
    trims.claim_for_fire.assert_awaited_once()


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
    trims.claim_for_fire = AsyncMock(return_value=True)
    trims.release_claim = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=10**9)
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=2, rung=1,
        threshold_pct=0.05, trim_pct=0.40, current_price=105.0,
    )
    assert fired is True
    placed = gw.place_order.call_args[0][1]
    assert placed.quantity >= 1


@pytest.mark.asyncio
async def test_skips_when_claim_fails():
    """If another tick already claimed the rung, fire_rung_if_crossed must
    short-circuit before placing any broker order."""
    gw = MagicMock()
    gw.place_order = AsyncMock()
    gw.qualify_equity = AsyncMock()
    trims = MagicMock()
    trims.claim_for_fire = AsyncMock(return_value=False)
    trims.record_fire = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=10**9)
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=105.0,
    )
    assert fired is False
    gw.place_order.assert_not_awaited()
    trims.record_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_trim_records_real_qty_and_cancels_residual():
    # A trim limit that partially fills (TIMED_OUT_PENDING, 25 of 40) must record
    # the real sold qty (not 0) and cancel the residual working sell order.
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=contract)
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.cancel_order = AsyncMock(return_value=True)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.TIMED_OUT_PENDING, broker_order_id="sell-p", perm_id=9,
        submitted_qty=40, filled_qty=25, remaining_qty=15, avg_fill_price=104.0,
        last_status="Submitted", status_timestamp="2026-05-05T14:00:00Z",
    ))
    trims = MagicMock()
    trims.record_fire = AsyncMock()
    trims.claim_for_fire = AsyncMock(return_value=True)
    trims.release_claim = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=10**9)
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits, intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100, rung=1,
        threshold_pct=0.05, trim_pct=0.40, current_price=105.0,
    )
    assert fired is True
    assert trims.record_fire.call_args.kwargs["sold_qty"] == 25
    gw.cancel_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_fill_trim_releases_rung_for_retry():
    # A trim limit that does not fill at all must NOT consume the rung: release
    # the claim so a later tick can retry, and do not record a fire.
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=contract)
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.cancel_order = AsyncMock(return_value=True)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.TIMED_OUT_PENDING, broker_order_id="sell-z", perm_id=9,
        submitted_qty=40, filled_qty=0, remaining_qty=40, avg_fill_price=None,
        last_status="Submitted", status_timestamp="2026-05-05T14:00:00Z",
    ))
    trims = MagicMock()
    trims.record_fire = AsyncMock()
    trims.claim_for_fire = AsyncMock(return_value=True)
    trims.release_claim = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=10**9)
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits, intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100, rung=1,
        threshold_pct=0.05, trim_pct=0.40, current_price=105.0,
    )
    assert fired is False
    trims.record_fire.assert_not_awaited()
    trims.release_claim.assert_awaited_once()
    gw.cancel_order.assert_awaited_once()


def test_in_rth():
    """_in_rth must use timezone-aware datetimes and handle DST correctly.
    ZoneInfo('America/New_York') handles EST/EDT automatically."""
    et = ZoneInfo("America/New_York")
    # RTH boundaries (inclusive open at 9:30, exclusive close at 16:00)
    assert _in_rth(datetime(2026, 5, 5, 9, 30, tzinfo=et))   # open of RTH
    assert _in_rth(datetime(2026, 5, 5, 10, 0, tzinfo=et))   # mid-morning
    assert _in_rth(datetime(2026, 5, 5, 15, 59, tzinfo=et))  # last minute of RTH
    assert not _in_rth(datetime(2026, 5, 5, 9, 0, tzinfo=et))   # pre-market
    assert not _in_rth(datetime(2026, 5, 5, 9, 29, tzinfo=et))  # one minute before open
    assert not _in_rth(datetime(2026, 5, 5, 16, 0, tzinfo=et))  # market close
    assert not _in_rth(datetime(2026, 5, 5, 20, 0, tzinfo=et))  # after hours


@pytest.mark.asyncio
async def test_does_not_oversell_after_position_exited():
    """After the trader is followed out (remaining held = 0), a trim rung must
    NOT place a SELL — otherwise the ladder shorts a position we no longer own.
    The held-shares check happens BEFORE claiming the rung so it is left intact
    for a later tick rather than silently consumed."""
    gw = MagicMock()
    gw.qualify_equity = AsyncMock()
    gw.place_order = AsyncMock()
    trims = MagicMock()
    trims.claim_for_fire = AsyncMock(return_value=True)
    trims.record_fire = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=0)  # followed out / fully trimmed
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=105.0,
    )
    assert fired is False
    gw.place_order.assert_not_awaited()
    trims.claim_for_fire.assert_not_awaited()  # guarded before the claim
    trims.record_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_caps_trim_qty_at_remaining_held():
    """A trim sized at 0.40 * 100 = 40 must be capped at the shares still held
    (10) when the position has been partly exited — never sell into a short."""
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=contract)
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="sell-cap", perm_id=11,
        submitted_qty=10, filled_qty=10, remaining_qty=0, avg_fill_price=105.0,
        last_status="Filled", status_timestamp="2026-05-05T14:00:00Z",
    ))
    trims = MagicMock()
    trims.claim_for_fire = AsyncMock(return_value=True)
    trims.record_fire = AsyncMock()
    trims.release_claim = AsyncMock()
    exits = MagicMock()
    exits.remaining_qty = AsyncMock(return_value=10)  # only 10 shares left
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims, exits_store=exits,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=105.0,
    )
    assert fired is True
    placed = gw.place_order.call_args[0][1]
    assert placed.quantity == 10  # capped at remaining held, not the nominal 40
