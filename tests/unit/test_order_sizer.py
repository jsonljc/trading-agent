import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.order_sizer import OrderSizer
from infra.ib.models import AccountSummary, BrokerContractRef, OptionCandidate
from infra.ib.gateway import IBGatewayUnavailable


def _policy(low_pct=0.05, high_pct=0.10):
    p = MagicMock()
    p.sizing_policy.low_conviction_pct = low_pct
    p.sizing_policy.high_conviction_pct = high_pct
    return p


def _ctx(instrument_type="option", conviction="high", ask=5.0, multiplier=100):
    c = Context(trace_id="t", event_id="e")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    candidate = OptionCandidate(symbol="AAPL", expiry="2026-12-18", strike=150.0,
                                 right="C", bid=ask-0.5, ask=ask, mid=ask-0.25,
                                 spread_pct=0.1, open_interest=100, volume=50,
                                 multiplier=multiplier, contract_ref=ref)
    c.update({
        "instrument_type": instrument_type,
        "ticker": "AAPL",
        "conviction_bucket": conviction,
        "option_candidates": [candidate],
        "selected_contract": ref,
        "selected_strike": 150.0,
    })
    return c


def _gateway(buying_power=100_000.0):
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=buying_power, net_liquidation=buying_power, currency="USD"
    ))
    gw.get_quote = AsyncMock(return_value=150.0)
    return gw


@pytest.mark.asyncio
async def test_high_conviction_option_sizing():
    # 10% of 100k = 10k; ask=5.0, multiplier=100 → cost=500/contract; qty=20
    sizer = OrderSizer(_policy(), _gateway(100_000))
    result = await sizer.run(_ctx(instrument_type="option", conviction="high", ask=5.0))
    assert result.status == "success"
    assert result.updates["quantity"] == 20
    assert "high_conviction" in result.updates["sizing_reason"]


@pytest.mark.asyncio
async def test_low_conviction_option_sizing():
    # 5% of 100k = 5k; ask=5.0, multiplier=100 → cost=500/contract; qty=10
    sizer = OrderSizer(_policy(), _gateway(100_000))
    result = await sizer.run(_ctx(conviction="low", ask=5.0))
    assert result.status == "success"
    assert result.updates["quantity"] == 10


@pytest.mark.asyncio
async def test_insufficient_buying_power_fails():
    # 10% of 100 = 10; ask=5.0, multiplier=100 → cost=500; qty=0 → fail
    sizer = OrderSizer(_policy(), _gateway(100))
    result = await sizer.run(_ctx(ask=5.0))
    assert result.status == "fail"
    assert "insufficient_buying_power" in result.reason


@pytest.mark.asyncio
async def test_gateway_unavailable_fails():
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(side_effect=IBGatewayUnavailable("down"))
    sizer = OrderSizer(_policy(), gw)
    result = await sizer.run(_ctx())
    assert result.status == "fail"
    assert "broker_unavailable" in result.reason


@pytest.mark.asyncio
async def test_equity_sizing_uses_get_quote():
    gw = _gateway(100_000)
    gw.get_quote = AsyncMock(return_value=200.0)
    sizer = OrderSizer(_policy(), gw)
    ctx = _ctx(instrument_type="equity", conviction="high")
    result = await sizer.run(ctx)
    # 10% of 100k = 10k / 200 = 50 shares
    assert result.status == "success"
    assert result.updates["quantity"] == 50
