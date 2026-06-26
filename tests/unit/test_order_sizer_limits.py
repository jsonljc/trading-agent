import pytest
from agent.context import Context
from skills.execution.order_sizer import OrderSizer


class FakeAccount:
    def __init__(self, net_liq, buying_power):
        self.net_liquidation = net_liq
        self.buying_power = buying_power


class FakeGateway:
    def __init__(self, net_liq=100_000.0, buying_power=200_000.0, quote=100.0):
        self._account = FakeAccount(net_liq, buying_power)
        self._quote = quote

    async def get_account_summary(self): return self._account
    async def get_quote(self, ticker): return self._quote


def _equity_ctx(**extra):
    data = {"instrument_type": "equity", "ticker": "X", "shares_pct": 0.50}
    data.update(extra)
    return Context(trace_id="t", event_id="e", data=data)


@pytest.mark.asyncio
async def test_buying_power_clamps_quantity():
    # base = 100k * 2 = 200k; pct 0.50 -> alloc 100k; quote 100 -> 1000 sh desired.
    # buying_power 40k -> clamp to floor(40000/100) = 400 sh, notional 40k.
    sizer = OrderSizer(FakeGateway(net_liq=100_000, buying_power=40_000, quote=100.0),
                       margin_multiplier=2.0)
    result = await sizer.run(_equity_ctx())
    assert result.status == "success"
    assert result.updates["quantity"] == 400
    assert result.updates["notional_estimate"] == pytest.approx(40_000.0)
    assert result.updates["capped_by"] == "buying_power"


@pytest.mark.asyncio
async def test_buying_power_too_small_skips():
    # buying_power 50 < one share at 100 -> skip
    sizer = OrderSizer(FakeGateway(net_liq=100_000, buying_power=50.0, quote=100.0),
                       margin_multiplier=2.0)
    result = await sizer.run(_equity_ctx())
    assert result.status == "skip"
    assert "insufficient_buying_power" in (result.reason or "")


@pytest.mark.asyncio
async def test_aggregate_cap_skips():
    # desired notional 100k <= bp 200k, but open 50k + 100k = 150k > cap 120k -> skip
    sizer = OrderSizer(FakeGateway(net_liq=100_000, buying_power=200_000, quote=100.0),
                       margin_multiplier=2.0)
    ctx = _equity_ctx(open_exposure=50_000.0, aggregate_notional_cap=120_000.0)
    result = await sizer.run(ctx)
    assert result.status == "skip"
    assert "exposure_cap_exceeded" in (result.reason or "")


@pytest.mark.asyncio
async def test_aggregate_cap_skips_even_when_shares_unfilled():
    # no shares_intent_id -> partial_or returns a real skip (halts the entry)
    sizer = OrderSizer(FakeGateway(net_liq=100_000, buying_power=200_000, quote=100.0),
                       margin_multiplier=2.0)
    ctx = _equity_ctx(open_exposure=119_999.0, aggregate_notional_cap=120_000.0)
    result = await sizer.run(ctx)
    assert result.status == "skip"


@pytest.mark.asyncio
async def test_within_all_limits_passes_and_tallies():
    # pct 0.05 -> alloc 10k -> 100 sh @100 = 10k; bp 200k ok; open 50k + 10k <= cap 120k
    sizer = OrderSizer(FakeGateway(net_liq=100_000, buying_power=200_000, quote=100.0),
                       margin_multiplier=2.0)
    ctx = _equity_ctx(shares_pct=0.05, open_exposure=50_000.0,
                      aggregate_notional_cap=120_000.0)
    result = await sizer.run(ctx)
    assert result.status == "success"
    assert result.updates["quantity"] == 100
    assert result.updates["capped_by"] is None
    # running tally so a later leg sees this leg's deployment
    assert result.updates["open_exposure"] == pytest.approx(60_000.0)


@pytest.mark.asyncio
async def test_no_guard_keys_means_no_aggregate_check():
    # Without ExposureGuard keys present, the aggregate check is inert; buying
    # power is still enforced from the live account.
    sizer = OrderSizer(FakeGateway(net_liq=100_000, buying_power=200_000, quote=100.0),
                       margin_multiplier=2.0)
    result = await sizer.run(_equity_ctx(shares_pct=0.05))
    assert result.status == "success"
    assert result.updates["quantity"] == 100
    assert "open_exposure" not in result.updates
