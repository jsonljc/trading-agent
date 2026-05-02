import pytest
from agent.context import Context
from skills.execution.order_sizer import OrderSizer


class FakeAccount:
    def __init__(self, bp): self.buying_power = bp


class FakeGateway:
    def __init__(self, bp=100_000.0, quote=10.0):
        self._account = FakeAccount(bp)
        self._quote = quote
    async def get_account_summary(self): return self._account
    async def get_quote(self, ticker): return self._quote


@pytest.mark.asyncio
async def test_order_sizer_uses_size_pct_from_ctx_for_stock():
    sizer = OrderSizer(gateway=FakeGateway(bp=100_000, quote=20.0))
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "stock", "ticker": "X",
        "size_pct": 0.05,
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    # 5% of 100k = 5000; at $20 → 250 shares
    assert result.updates["quantity"] == 250
    assert "size_pct=0.05" in result.updates["sizing_reason"]


@pytest.mark.asyncio
async def test_order_sizer_uses_size_pct_for_options():
    """Confirm options path also reads size_pct from ctx."""
    class FakeCandidate:
        def __init__(self, strike, ask, multiplier):
            self.strike = strike
            self.ask = ask
            self.multiplier = multiplier

    sizer = OrderSizer(gateway=FakeGateway(bp=10_000))
    candidate = FakeCandidate(strike=100.0, ask=2.50, multiplier=100)
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "option",
        "size_pct": 0.10,
        "option_candidates": [candidate],
        "selected_strike": 100.0,
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    # 10% of 10k = 1000; cost per contract = 2.50 * 100 = 250; → 4 contracts
    assert result.updates["quantity"] == 4


@pytest.mark.asyncio
async def test_order_sizer_fails_when_size_pct_missing_from_ctx():
    sizer = OrderSizer(gateway=FakeGateway())
    ctx = Context(trace_id="t", event_id="e", data={"instrument_type": "stock", "ticker": "X"})
    result = await sizer.run(ctx)
    assert result.status == "fail"
    assert "size_pct" in (result.reason or "")


@pytest.mark.asyncio
async def test_order_sizer_fails_when_size_pct_zero_or_negative():
    sizer = OrderSizer(gateway=FakeGateway())
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "stock", "ticker": "X", "size_pct": 0.0,
    })
    result = await sizer.run(ctx)
    assert result.status == "fail"
    assert "size_pct" in (result.reason or "")
