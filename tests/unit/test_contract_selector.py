import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock
from agent.context import Context
from skills.execution.contract_selector import ContractSelector
from infra.ib.models import OptionCandidate, BrokerContractRef


def _policy(min_expiry_days=180, min_bid=0.01, max_spread_pct=0.40, strike_policy="closest_itm_call"):
    p = MagicMock()
    p.instrument_policy.min_expiry_days = min_expiry_days
    p.instrument_policy.strike_policy = strike_policy
    p.pricing_policy_guards.min_bid = min_bid
    p.pricing_policy_guards.max_spread_pct = max_spread_pct
    return p


def _candidate(strike, expiry_days=200, spread_pct=0.10, bid=5.0, ask=5.5):
    expiry = (date.today() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", expiry=expiry.replace("-", ""),
                             strike=strike, right="C", qualified=True)
    mid = (bid + ask) / 2
    return OptionCandidate(symbol="AAPL", expiry=expiry, strike=strike, right="C",
                            bid=bid, ask=ask, mid=mid, spread_pct=spread_pct,
                            open_interest=100, volume=50, multiplier=100, contract_ref=ref)


def _ctx(instrument_type="option", candidates=None, ticker="AAPL", spot=155.0):
    c = Context(trace_id="t", event_id="e")
    c.update({
        "instrument_type": instrument_type,
        "option_candidates": candidates or [],
        "ticker": ticker,
        "spot_price": spot,
    })
    return c


@pytest.mark.asyncio
async def test_selects_closest_itm_call(db=None):
    # spot=155, ITM calls are strike < 155; closest ITM = 150
    candidates = [_candidate(140), _candidate(150), _candidate(160)]
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "success"
    assert result.updates["selected_strike"] == 150.0


@pytest.mark.asyncio
async def test_rejects_short_expiry():
    candidates = [_candidate(150, expiry_days=30)]  # below min_expiry_days=180
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "fail"
    assert "no_eligible_contract" in result.reason


@pytest.mark.asyncio
async def test_equity_fallback_returns_stk_contract():
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(instrument_type="equity"))
    assert result.status == "success"
    assert result.updates["selected_contract"].sec_type == "STK"
    assert result.updates["selected_contract"].qualified is False


@pytest.mark.asyncio
async def test_rejects_low_bid():
    candidates = [_candidate(150, bid=0.005, ask=0.01)]
    selector = ContractSelector(_policy(min_bid=0.01))
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "fail"
    assert "no_eligible_contract" in result.reason
