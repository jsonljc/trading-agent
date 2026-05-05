import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.equity_contract_builder import EquityContractBuilder
from agent.context import Context
from infra.ib.models import BrokerContractRef


@pytest.mark.asyncio
async def test_builds_qualified_stk_contract():
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        con_id=12345, qualified=True,
    ))
    builder = EquityContractBuilder(gw)
    ctx = Context(trace_id="t", event_id="e")
    ctx.update({"ticker": "AAPL"})
    result = await builder.run(ctx)
    assert result.status == "success"
    selected = result.updates["selected_contract"]
    assert selected.symbol == "AAPL"
    assert selected.sec_type == "STK"
    assert selected.qualified is True
    assert selected.con_id == 12345
    assert result.updates["instrument_type"] == "equity"


@pytest.mark.asyncio
async def test_qualify_failure_returns_fail():
    from infra.ib.gateway import IBGatewayUnavailable
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(side_effect=IBGatewayUnavailable("nope"))
    builder = EquityContractBuilder(gw)
    ctx = Context(trace_id="t", event_id="e")
    ctx.update({"ticker": "BADTKR"})
    result = await builder.run(ctx)
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_missing_ticker_returns_fail():
    gw = MagicMock()
    builder = EquityContractBuilder(gw)
    ctx = Context(trace_id="t", event_id="e")
    result = await builder.run(ctx)
    assert result.status == "fail"
    assert "ticker missing" in result.reason
