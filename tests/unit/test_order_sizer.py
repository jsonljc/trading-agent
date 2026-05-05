import pytest
from agent.context import Context
from skills.execution.order_sizer import OrderSizer


class FakeAccount:
    def __init__(self, net_liq, bp=None):
        self.net_liquidation = net_liq
        # buying_power kept for completeness but OrderSizer no longer reads it
        self.buying_power = bp if bp is not None else net_liq


class FakeGateway:
    def __init__(self, net_liq=100_000.0, quote=10.0):
        self._account = FakeAccount(net_liq)
        self._quote = quote

    async def get_account_summary(self): return self._account
    async def get_quote(self, ticker): return self._quote


@pytest.mark.asyncio
async def test_order_sizer_uses_shares_pct_from_ctx_for_equity():
    # net_liq=50_000, margin_multiplier=2.0 → base=100_000
    # shares_pct=0.05 → alloc=5_000; quote=20 → 250 shares
    sizer = OrderSizer(gateway=FakeGateway(net_liq=50_000, quote=20.0),
                       margin_multiplier=2.0)
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "equity", "ticker": "X",
        "shares_pct": 0.05,
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    assert result.updates["quantity"] == 250
    assert "NetLiq" in result.updates["sizing_reason"]
    assert "equity" in result.updates["sizing_reason"]


@pytest.mark.asyncio
async def test_order_sizer_uses_options_pct_for_options():
    """Confirm options path reads options_pct from ctx."""
    class FakeCandidate:
        def __init__(self, strike, ask, multiplier):
            self.strike = strike
            self.ask = ask
            self.multiplier = multiplier

    # net_liq=5_000, margin_multiplier=2.0 → base=10_000
    # options_pct=0.10 → alloc=1_000; cost_per_contract=2.50*100=250 → 4 contracts
    sizer = OrderSizer(gateway=FakeGateway(net_liq=5_000), margin_multiplier=2.0)
    candidate = FakeCandidate(strike=100.0, ask=2.50, multiplier=100)
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "option",
        "options_pct": 0.10,
        "option_candidates": [candidate],
        "selected_strike": 100.0,
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    assert result.updates["quantity"] == 4


@pytest.mark.asyncio
async def test_order_sizer_fails_when_pct_missing_from_ctx():
    sizer = OrderSizer(gateway=FakeGateway(), margin_multiplier=2.0)
    ctx = Context(trace_id="t", event_id="e", data={"instrument_type": "equity", "ticker": "X"})
    result = await sizer.run(ctx)
    assert result.status == "fail"
    assert "pct missing" in (result.reason or "")


@pytest.mark.asyncio
async def test_order_sizer_fails_when_pct_zero_or_negative():
    sizer = OrderSizer(gateway=FakeGateway(), margin_multiplier=2.0)
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "equity", "ticker": "X", "shares_pct": 0.0,
    })
    result = await sizer.run(ctx)
    assert result.status == "fail"
    assert "pct missing" in (result.reason or "")
