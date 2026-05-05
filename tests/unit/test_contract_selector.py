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
async def test_uses_reference_price_when_spot_price_missing():
    # Regression: spot_price is unset by the live chain. Selector must fall
    # back to reference_price (set by ReferencePriceCapture upstream),
    # otherwise it always picks the cheapest OTM call (strike < 0.0 == empty
    # ITM set) regardless of where price actually is.
    candidates = [_candidate(140), _candidate(150), _candidate(160)]
    selector = ContractSelector(_policy())
    ctx = Context(trace_id="t", event_id="e")
    ctx.update({
        "option_candidates": candidates,
        "ticker": "AAPL",
        "reference_price": 155.0,  # spot_price intentionally absent
    })
    result = await selector.run(ctx)
    assert result.status == "success"
    # Without the fallback, this would be 140 (lowest-strike OTM at spot=0).
    assert result.updates["selected_strike"] == 150.0


@pytest.mark.asyncio
async def test_partial_when_shares_already_filled():
    # Once shares_intent_id is set, no eligible contract → partial-success, not fail.
    selector = ContractSelector(_policy())
    ctx = _ctx(candidates=[], spot=155.0)
    ctx.update({"shares_intent_id": "shares-1"})
    result = await selector.run(ctx)
    assert result.status == "success"
    assert result.updates["partial_execution_reason"].startswith("no_eligible_contract")


@pytest.mark.asyncio
async def test_rejects_low_bid():
    candidates = [_candidate(150, bid=0.005, ask=0.01)]
    selector = ContractSelector(_policy(min_bid=0.01))
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "fail"
    assert "no_eligible_contract" in result.reason


@pytest.mark.asyncio
async def test_rejects_wide_spread():
    candidates = [_candidate(150, spread_pct=0.50)]  # above max_spread_pct=0.40
    selector = ContractSelector(_policy(max_spread_pct=0.40))
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "fail"
    assert "no_eligible_contract" in result.reason


@pytest.mark.asyncio
async def test_otm_fallback_when_no_itm():
    # spot=100, all candidates have strike >= spot → no ITM, fallback to nearest OTM
    candidates = [_candidate(110), _candidate(120), _candidate(130)]
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(candidates=candidates, spot=100.0))
    assert result.status == "success"
    assert result.updates["selected_strike"] == 110.0  # lowest OTM


@pytest.mark.asyncio
async def test_empty_candidates_fails():
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(candidates=[], spot=155.0))
    assert result.status == "fail"
    assert "no_eligible_contract" in result.reason
