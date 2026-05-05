import pytest
from unittest.mock import AsyncMock, MagicMock
from infra.ib.models import AccountSummary
from skills.execution.order_sizer import OrderSizer
from agent.context import Context


@pytest.fixture
def gateway():
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=99999, net_liquidation=100_000.0, currency="USD",
    ))
    gw.get_quote = AsyncMock(return_value=200.0)
    return gw


@pytest.mark.asyncio
async def test_equity_uses_shares_pct_and_netliq_x_multiplier(gateway):
    sizer = OrderSizer(gateway, margin_multiplier=2.0)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "ticker": "AAPL", "instrument_type": "equity",
        "shares_pct": 0.10, "options_pct": 0.05,
    })
    result = await sizer.run(ctx)
    ctx.update(result.updates)
    # base = 100000 * 2.0 = 200000; alloc = 200000 * 0.10 = 20000; px=200 → qty=100
    assert result.status == "success"
    assert ctx.get("quantity") == 100
    assert ctx.get("notional_estimate") == pytest.approx(20000.0)


@pytest.mark.asyncio
async def test_option_uses_options_pct(gateway):
    sizer = OrderSizer(gateway, margin_multiplier=2.0)
    candidate = MagicMock(strike=180.0, ask=5.0, multiplier=100)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "ticker": "AAPL", "instrument_type": "option",
        "shares_pct": 0.10, "options_pct": 0.05,
        "option_candidates": [candidate], "selected_strike": 180.0,
    })
    result = await sizer.run(ctx)
    ctx.update(result.updates)
    # base = 200000; alloc = 200000 * 0.05 = 10000; cost = 5*100 = 500; qty = 20
    assert result.status == "success"
    assert ctx.get("quantity") == 20


@pytest.mark.asyncio
async def test_equity_ignores_options_pct(gateway):
    """Equity branch must read shares_pct, not options_pct."""
    sizer = OrderSizer(gateway, margin_multiplier=1.0)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "ticker": "AAPL", "instrument_type": "equity",
        "shares_pct": 0.20, "options_pct": 0.99,
    })
    result = await sizer.run(ctx)
    ctx.update(result.updates)
    # base = 100000 * 1.0 = 100000; alloc = 100000 * 0.20 = 20000; px=200 → qty=100
    assert result.status == "success"
    assert ctx.get("quantity") == 100


@pytest.mark.asyncio
async def test_option_ignores_shares_pct(gateway):
    """Option branch must read options_pct, not shares_pct."""
    sizer = OrderSizer(gateway, margin_multiplier=1.0)
    candidate = MagicMock(strike=180.0, ask=5.0, multiplier=100)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "ticker": "AAPL", "instrument_type": "option",
        "shares_pct": 0.99, "options_pct": 0.05,
        "option_candidates": [candidate], "selected_strike": 180.0,
    })
    result = await sizer.run(ctx)
    ctx.update(result.updates)
    # base = 100000 * 1.0 = 100000; alloc = 100000 * 0.05 = 5000; cost = 500; qty = 10
    assert result.status == "success"
    assert ctx.get("quantity") == 10


@pytest.mark.asyncio
async def test_sizing_reason_mentions_netliq_and_multiplier(gateway):
    """sizing_reason log string must reference NetLiq and multiplier."""
    sizer = OrderSizer(gateway, margin_multiplier=2.0)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "ticker": "AAPL", "instrument_type": "equity",
        "shares_pct": 0.10, "options_pct": 0.05,
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    reason = result.updates.get("sizing_reason", "")
    assert "NetLiq" in reason
    assert "2.0" in reason


@pytest.mark.asyncio
async def test_fails_when_shares_pct_missing_for_equity(gateway):
    sizer = OrderSizer(gateway, margin_multiplier=2.0)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"ticker": "AAPL", "instrument_type": "equity", "options_pct": 0.05})
    result = await sizer.run(ctx)
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_fails_when_options_pct_missing_for_option(gateway):
    candidate = MagicMock(strike=180.0, ask=5.0, multiplier=100)
    sizer = OrderSizer(gateway, margin_multiplier=2.0)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "ticker": "AAPL", "instrument_type": "option",
        "shares_pct": 0.10,
        "option_candidates": [candidate], "selected_strike": 180.0,
    })
    result = await sizer.run(ctx)
    assert result.status == "fail"
