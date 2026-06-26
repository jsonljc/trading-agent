import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.order_sizer import OrderSizer


@pytest.mark.asyncio
async def test_order_sizer_reuses_reference_price_without_refetching():
    """On the hot path the equity quote is already captured by
    ReferencePriceCapture (ctx['reference_price']); OrderSizer must reuse it
    rather than issue a third redundant get_quote round-trip."""
    account = SimpleNamespace(net_liquidation=100_000.0, buying_power=500_000.0)
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(return_value=account)
    gw.get_quote = AsyncMock(return_value=50.0)
    sizer = OrderSizer(gw, margin_multiplier=2.0)
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "equity",
        "shares_pct": 0.10,
        "ticker": "AVEX",
        "reference_price": 50.0,
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    gw.get_quote.assert_not_awaited()        # reused the captured reference price
    assert result.updates["quantity"] == 400  # floor(100k * 2.0 * 0.10 / 50)


@pytest.mark.asyncio
async def test_order_sizer_falls_back_to_quote_when_no_reference():
    """A composition without ReferencePriceCapture upstream must still size by
    fetching the quote — reuse is an optimization, not a hard dependency."""
    account = SimpleNamespace(net_liquidation=100_000.0, buying_power=500_000.0)
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(return_value=account)
    gw.get_quote = AsyncMock(return_value=50.0)
    sizer = OrderSizer(gw, margin_multiplier=2.0)
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "equity",
        "shares_pct": 0.10,
        "ticker": "AVEX",
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    gw.get_quote.assert_awaited_once()
    assert result.updates["quantity"] == 400
