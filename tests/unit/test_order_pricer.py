import pytest
from unittest.mock import MagicMock
from agent.context import Context
from skills.execution.order_pricer import OrderPricer
from infra.ib.models import BrokerContractRef, OptionCandidate


def _policy(spread_fraction=0.25, stock_buffer_pct=0.001, min_bid=0.01,
            max_spread_pct=0.40, max_equity_price=500.0):
    p = MagicMock()
    p.pricing_policy.option_spread_fraction = spread_fraction
    p.pricing_policy.stock_buffer_pct = stock_buffer_pct
    p.pricing_policy_guards.min_bid = min_bid
    p.pricing_policy_guards.max_spread_pct = max_spread_pct
    p.execution.max_equity_price = max_equity_price
    return p


def _ctx_option(bid=5.0, ask=5.5, spread_pct=0.09, selected_strike=150.0):
    c = Context(trace_id="t", event_id="e")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    candidate = OptionCandidate(symbol="AAPL", expiry="2026-12-18", strike=selected_strike,
                                 right="C", bid=bid, ask=ask, mid=(bid+ask)/2,
                                 spread_pct=spread_pct, open_interest=100, volume=50,
                                 multiplier=100, contract_ref=ref)
    c.update({"instrument_type": "option", "option_candidates": [candidate],
               "selected_strike": selected_strike})
    return c


def _ctx_equity(ask=150.0):
    c = Context(trace_id="t", event_id="e")
    c.update({"instrument_type": "equity", "ticker": "AAPL", "_equity_ask": ask})
    return c


@pytest.mark.asyncio
async def test_option_limit_price():
    # mid=5.25, spread_fraction=0.25 → price = 5.25 + (5.5-5.25)*0.25 = 5.3125 → 5.31
    pricer = OrderPricer(_policy())
    result = await pricer.run(_ctx_option(bid=5.0, ask=5.5))
    assert result.status == "success"
    assert result.updates["limit_price"] == 5.31
    assert result.updates["order_type"] == "LMT"


@pytest.mark.asyncio
async def test_option_fails_low_bid():
    pricer = OrderPricer(_policy(min_bid=0.01))
    result = await pricer.run(_ctx_option(bid=0.005, ask=0.01, spread_pct=0.5))
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_option_fails_high_spread():
    pricer = OrderPricer(_policy(max_spread_pct=0.40))
    result = await pricer.run(_ctx_option(bid=4.0, ask=7.0, spread_pct=0.43))
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_equity_limit_price():
    # ask=150, buffer=0.001 → 150 * 1.001 = 150.15
    pricer = OrderPricer(_policy())
    ctx = _ctx_equity(ask=150.0)
    result = await pricer.run(ctx)
    assert result.status == "success"
    assert result.updates["limit_price"] == 150.15


@pytest.mark.asyncio
async def test_equity_fails_above_max_price():
    pricer = OrderPricer(_policy(max_equity_price=500.0))
    ctx = _ctx_equity(ask=600.0)
    result = await pricer.run(ctx)
    assert result.status == "fail"
    assert "max_equity_price" in result.reason


@pytest.mark.asyncio
async def test_order_pricer_emits_initial_reference_ask():
    from datetime import date, timedelta
    from infra.ib.models import OptionCandidate, BrokerContractRef

    policy = MagicMock()
    policy.pricing_policy_guards.min_bid = 0.01
    policy.pricing_policy_guards.max_spread_pct = 0.40
    policy.pricing_policy.option_spread_fraction = 0.25
    policy.execution.max_equity_price = 500.0

    expiry = (date.today() + timedelta(days=200)).strftime("%Y-%m-%d")
    ref = BrokerContractRef(symbol="NVDA", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    candidate = OptionCandidate(
        symbol="NVDA", expiry=expiry, strike=150.0, right="C",
        bid=5.0, ask=5.50, mid=5.25, spread_pct=0.09,
        open_interest=100, volume=50, multiplier=100, contract_ref=ref,
    )

    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"instrument_type": "option", "option_candidates": [candidate],
                "selected_strike": 150.0})

    skill = OrderPricer(policy)
    result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates["initial_reference_ask"] == pytest.approx(5.50)
