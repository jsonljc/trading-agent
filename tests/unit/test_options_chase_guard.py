import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.options_chase_guard import OptionsChaseGuard
from agent.context import Context


def _gw(price):
    g = MagicMock()
    g.get_quote = AsyncMock(return_value=price)
    return g


@pytest.mark.asyncio
async def test_passes_when_within_threshold():
    g = OptionsChaseGuard(_gw(109.0), threshold_pct=0.10)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_passes_at_boundary():
    g = OptionsChaseGuard(_gw(110.0), threshold_pct=0.10)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_skips_above_threshold():
    g = OptionsChaseGuard(_gw(111.0), threshold_pct=0.10)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "skip"
    assert "options_chase_skip" in (result.reason or "")


@pytest.mark.asyncio
async def test_quote_failure_skips_options():
    """If we can't quote, skip the options leg rather than fail the whole chain."""
    from infra.ib.gateway import IBGatewayUnavailable
    gw = MagicMock()
    gw.get_quote = AsyncMock(side_effect=IBGatewayUnavailable("nope"))
    g = OptionsChaseGuard(gw, threshold_pct=0.10)
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "skip"
