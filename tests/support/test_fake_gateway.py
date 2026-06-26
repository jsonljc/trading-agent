import pytest

from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import FillStatus, PreparedOrder
from tests.support.fake_gateway import FakeGateway
from tests.support.factories import make_filled_intent


def _order(qty):
    return PreparedOrder(action="SELL", quantity=qty, order_type="LMT",
                         limit_price=99.0, tif="DAY")


@pytest.mark.asyncio
async def test_full_fill_returns_requested_qty():
    gw = FakeGateway()
    trade = await gw.place_order(None, _order(40), "coid")
    fill = await gw.wait_fill(trade, timeout=1.0)
    assert fill.status == FillStatus.FILLED
    assert fill.filled_qty == 40
    assert gw.placed[-1].quantity == 40


@pytest.mark.asyncio
async def test_partial_fill_uses_fraction_and_is_not_filled_status():
    gw = FakeGateway()
    gw.fill_mode = "partial"
    gw.partial_fraction = 0.5
    trade = await gw.place_order(None, _order(40), "coid")
    fill = await gw.wait_fill(trade, timeout=1.0)
    assert fill.filled_qty == 20
    assert fill.status != FillStatus.FILLED


@pytest.mark.asyncio
async def test_zero_fill():
    gw = FakeGateway()
    gw.fill_mode = "zero"
    trade = await gw.place_order(None, _order(40), "coid")
    fill = await gw.wait_fill(trade, timeout=1.0)
    assert fill.filled_qty == 0
    assert fill.avg_fill_price is None


@pytest.mark.asyncio
async def test_unavailable_raises_on_place():
    gw = FakeGateway()
    gw.unavailable = True
    with pytest.raises(IBGatewayUnavailable):
        await gw.place_order(None, _order(10), "coid")


@pytest.mark.asyncio
async def test_account_summary_shape():
    gw = FakeGateway(net_liquidation=250_000.0, buying_power=120_000.0)
    acct = await gw.get_account_summary()
    assert acct.net_liquidation == 250_000.0
    assert acct.buying_power == 120_000.0


@pytest.mark.asyncio
async def test_on_wait_fill_hook_runs_once():
    gw = FakeGateway()
    calls = []
    async def hook():
        calls.append(1)
    gw.on_wait_fill = hook
    trade1 = await gw.place_order(None, _order(10), "coid")
    await gw.wait_fill(trade1, timeout=1.0)
    trade2 = await gw.place_order(None, _order(10), "coid")
    await gw.wait_fill(trade2, timeout=1.0)
    assert calls == [1]  # one-shot


def test_make_filled_intent_orders_by_seq():
    a = make_filled_intent("e1:AAPL:long", channel="mystic", ticker="AAPL",
                           fill_qty=100, seq=1)
    b = make_filled_intent("e2:AAPL:long", channel="mystic", ticker="AAPL",
                           fill_qty=50, seq=2)
    assert a["filled_at"] < b["filled_at"]
    assert a["execution_state"] == "filled" and a["instrument_type"] == "equity"
