"""INV-4: OrderSizer never returns a GENUINE sizing success (one carrying a
`quantity` in updates) that exceeds buying power or the aggregate exposure cap,
and never one with quantity<1. Gate on `"quantity" in updates`, NOT status
(partial_or returns status='success' without a quantity). See spec §3 INV-4.
"""
import asyncio

from hypothesis import given
from hypothesis import strategies as st

from agent.context import Context
from skills.execution.order_sizer import OrderSizer
from tests.support.fake_gateway import FakeGateway

_TOL = 1e-6


@given(
    net_liq=st.floats(min_value=1_000.0, max_value=10_000_000.0),
    buying_power=st.floats(min_value=0.0, max_value=10_000_000.0),
    size_pct=st.floats(min_value=0.0001, max_value=1.0),
    price=st.floats(min_value=0.5, max_value=5_000.0),
    margin=st.sampled_from([1.0, 2.0]),
    exposure=st.one_of(
        st.none(),
        st.tuples(st.floats(min_value=0.0, max_value=5_000_000.0),   # open_exposure
                  st.floats(min_value=0.0, max_value=10_000_000.0)),  # aggregate_cap
    ),
)
def test_order_sizer_respects_caps(net_liq, buying_power, size_pct, price, margin, exposure):
    async def run():
        gw = FakeGateway(net_liquidation=net_liq, buying_power=buying_power)
        sizer = OrderSizer(gw, margin_multiplier=margin)
        ctx = Context(trace_id="t", event_id="e")
        ctx.update({"instrument_type": "equity", "shares_pct": size_pct,
                    "reference_price": price, "ticker": "AAPL"})
        if exposure is not None:
            open_exposure, agg_cap = exposure
            ctx.update({"open_exposure": open_exposure,
                        "aggregate_notional_cap": agg_cap})
        result = await sizer.run(ctx)
        return result, (exposure[0] if exposure else None), (exposure[1] if exposure else None)

    result, open_exposure_in, agg_cap = asyncio.new_event_loop().run_until_complete(run())

    # Only a GENUINE sizing success carries a quantity.
    if "quantity" not in result.updates:
        return
    qty = result.updates["quantity"]
    notional = result.updates["notional_estimate"]
    unit_cost = price

    # (a) quantity >= 1 always
    assert qty >= 1

    # (b) buying-power clamp (buying_power is always a float from AccountSummary)
    assert qty * unit_cost <= buying_power + _TOL, (
        f"notional {qty*unit_cost} > buying_power {buying_power}")

    # (c) aggregate exposure cap only when both ctx keys were present
    if open_exposure_in is not None and agg_cap is not None:
        assert open_exposure_in + notional <= agg_cap + _TOL, (
            f"open {open_exposure_in} + notional {notional} > cap {agg_cap}")
