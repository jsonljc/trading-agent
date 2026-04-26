import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.signal.ticker_validator import TickerValidator


def _ctx(ticker="NVDA", side="long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"ticker": ticker, "side": side})
    return ctx


def _gateway(qualifies=True):
    gw = MagicMock()
    if qualifies:
        ref = MagicMock()
        ref.qualified = True
        gw.qualify = AsyncMock(return_value=ref)
    else:
        from infra.ib.gateway import IBGatewayUnavailable
        gw.qualify = AsyncMock(side_effect=IBGatewayUnavailable("not found"))
    return gw


async def test_valid_ticker_passes():
    skill = TickerValidator(_gateway(qualifies=True))
    result = await skill.run(_ctx())
    assert result.status == "success"


async def test_unresolvable_ticker_fails():
    skill = TickerValidator(_gateway(qualifies=False))
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "ambiguous_signal" in result.reason


async def test_missing_ticker_fails():
    skill = TickerValidator(_gateway())
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"side": "long"})
    result = await skill.run(ctx)
    assert result.status == "fail"


async def test_ambiguous_side_fails():
    skill = TickerValidator(_gateway())
    result = await skill.run(_ctx(side="none"))
    assert result.status == "skip"
    assert "ambiguous_signal" in result.reason
