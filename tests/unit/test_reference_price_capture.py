import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.reference_price_capture import ReferencePriceCapture
from agent.context import Context


def _ctx(ticker="AAPL"):
    c = Context(trace_id="trace-1", event_id="sig-1")
    c.update({"ticker": ticker})
    return c


@pytest.mark.asyncio
async def test_captures_quote_into_context():
    gw = MagicMock()
    gw.get_quote = AsyncMock(return_value=147.32)
    skill = ReferencePriceCapture(gw)
    ctx = _ctx()
    result = await skill.run(ctx)
    assert result.status == "success"
    ctx.update(result.updates)
    assert ctx.get("reference_price") == 147.32


@pytest.mark.asyncio
async def test_quote_failure_aborts_chain():
    from infra.ib.gateway import IBGatewayUnavailable
    gw = MagicMock()
    gw.get_quote = AsyncMock(side_effect=IBGatewayUnavailable("no quote"))
    skill = ReferencePriceCapture(gw)
    ctx = _ctx()
    result = await skill.run(ctx)
    assert result.status == "fail"
    assert "reference_price_unavailable" in (result.reason or "")
