import pytest
from unittest.mock import MagicMock
from agent.context import Context
from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
from infra.ib.models import OptionCandidate, BrokerContractRef


def _policy(max_spread_pct=0.40):
    p = MagicMock()
    p.pricing_policy_guards.max_spread_pct = max_spread_pct
    return p


def _candidate(spread_pct=0.10):
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    return OptionCandidate(symbol="AAPL", expiry="2026-12-18", strike=150.0, right="C",
                            bid=4.8, ask=5.2, mid=5.0, spread_pct=spread_pct,
                            open_interest=100, volume=50, multiplier=100, contract_ref=ref)


def _ctx(session="rth", candidates=None):
    c = Context(trace_id="t", event_id="e")
    if candidates is None:
        candidates = [_candidate()]
    c.update({"execution_session": session, "option_candidates": candidates})
    return c


@pytest.mark.asyncio
async def test_rth_with_candidates_returns_option():
    guard = InstrumentMarketabilityGuard(_policy())
    result = await guard.run(_ctx(session="rth"))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "option"
    assert result.updates.get("fallback_reason") is None


@pytest.mark.asyncio
async def test_premarket_falls_back_to_equity():
    guard = InstrumentMarketabilityGuard(_policy())
    result = await guard.run(_ctx(session="premarket"))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "equity"
    assert result.updates["fallback_reason"] == "options_outside_rth"


@pytest.mark.asyncio
async def test_wide_spread_falls_back_to_equity():
    guard = InstrumentMarketabilityGuard(_policy(max_spread_pct=0.40))
    result = await guard.run(_ctx(session="rth", candidates=[_candidate(spread_pct=0.50)]))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "equity"
    assert result.updates["fallback_reason"] == "all_candidates_spread_too_wide"


@pytest.mark.asyncio
async def test_no_candidates_falls_back_to_equity():
    guard = InstrumentMarketabilityGuard(_policy())
    result = await guard.run(_ctx(session="rth", candidates=[]))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "equity"
    assert result.updates["fallback_reason"] == "no_option_candidates"
