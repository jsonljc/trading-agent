import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.chain_lookup import ChainLookup
from infra.ib.models import OptionCandidate, BrokerContractRef
from infra.ib.gateway import IBGatewayUnavailable


def _candidate(ticker="AAPL", strike=150.0):
    ref = BrokerContractRef(symbol=ticker, sec_type="OPT", exchange="SMART", currency="USD",
                             expiry="20261218", strike=strike, right="C", qualified=True)
    return OptionCandidate(symbol=ticker, expiry="2026-12-18", strike=strike, right="C",
                            bid=5.0, ask=5.5, mid=5.25, spread_pct=0.09,
                            open_interest=100, volume=50, multiplier=100, contract_ref=ref)


def _ctx(signal_id="sig-1", trace_id="trace-1", ticker="AAPL"):
    c = Context(trace_id=trace_id, event_id=signal_id)
    c.update({"signal_id": signal_id, "ticker": ticker})
    return c


@pytest.mark.asyncio
async def test_chain_lookup_success(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(return_value=[_candidate()])
    skill = ChainLookup(gateway, db)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert len(result.updates["option_candidates"]) == 1
    assert result.updates["option_candidates"][0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_chain_lookup_empty_continues(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(return_value=[])
    skill = ChainLookup(gateway, db)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["option_candidates"] == []


@pytest.mark.asyncio
async def test_chain_lookup_gateway_unavailable_fails(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(side_effect=IBGatewayUnavailable("circuit open"))
    skill = ChainLookup(gateway, db)
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "broker_unavailable" in result.reason


@pytest.mark.asyncio
async def test_chain_lookup_persists_with_trace_id(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(return_value=[_candidate()])
    skill = ChainLookup(gateway, db)
    await skill.run(_ctx(trace_id="trace-xyz"))
    async with db.execute("SELECT trace_id FROM option_candidates") as cur:
        row = await cur.fetchone()
    assert row["trace_id"] == "trace-xyz"
