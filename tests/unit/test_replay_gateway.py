import pytest

from agent.replay.gateway import ReplayGateway
from infra.ib.models import BrokerContractRef, PreparedOrder, FillStatus, AccountSummary


@pytest.mark.asyncio
async def test_qualify_equity_returns_qualified_ref():
    gw = ReplayGateway()
    ref = await gw.qualify_equity("AAPL")
    assert isinstance(ref, BrokerContractRef)
    assert ref.symbol == "AAPL"
    assert ref.sec_type == "STK"
    assert ref.qualified is True


@pytest.mark.asyncio
async def test_qualify_marks_ref_qualified():
    gw = ReplayGateway()
    ref = BrokerContractRef(symbol="MSFT", sec_type="STK", exchange="SMART", currency="USD")
    out = await gw.qualify(ref)
    assert out.qualified is True
    assert out.symbol == "MSFT"


@pytest.mark.asyncio
async def test_get_quote_default_and_per_ticker_override():
    gw = ReplayGateway(quote=100.0, quotes={"NVDA": 250.5})
    assert await gw.get_quote("AAPL") == 100.0
    assert await gw.get_quote("NVDA") == 250.5


@pytest.mark.asyncio
async def test_get_account_summary_returns_fixed_net_liq():
    gw = ReplayGateway(net_liq=250_000.0)
    summ = await gw.get_account_summary()
    assert isinstance(summ, AccountSummary)
    assert summ.net_liquidation == 250_000.0
    assert summ.buying_power == 250_000.0


@pytest.mark.asyncio
async def test_get_chain_returns_empty():
    gw = ReplayGateway()
    assert await gw.get_chain("AAPL") == []


@pytest.mark.asyncio
async def test_place_order_records_and_wait_fill_is_filled():
    gw = ReplayGateway()
    ref = await gw.qualify_equity("AAPL")
    order = PreparedOrder(action="BUY", quantity=10, order_type="LMT",
                          limit_price=99.0, tif="DAY")
    trade = await gw.place_order(ref, order, "trace:shares:evt")
    assert len(gw.placed_orders) == 1
    rec = gw.placed_orders[0]
    assert rec["action"] == "BUY"
    assert rec["quantity"] == 10
    assert rec["order_type"] == "LMT"
    assert rec["limit_price"] == 99.0
    assert rec["instrument"] == "AAPL"
    assert rec["client_order_id"] == "trace:shares:evt"

    fill = await gw.wait_fill(trade, timeout=5.0)
    assert fill.status == FillStatus.FILLED
    assert fill.filled_qty == 10
    assert fill.remaining_qty == 0
    assert fill.avg_fill_price == 99.0


@pytest.mark.asyncio
async def test_market_order_fills_at_quote():
    gw = ReplayGateway(quote=42.0)
    ref = await gw.qualify_equity("F")
    order = PreparedOrder(action="BUY", quantity=3, order_type="MKT",
                          limit_price=None, tif="DAY")
    trade = await gw.place_order(ref, order, "cid")
    fill = await gw.wait_fill(trade, timeout=5.0)
    assert fill.status == FillStatus.FILLED
    assert fill.filled_qty == 3
    assert fill.avg_fill_price == 42.0


@pytest.mark.asyncio
async def test_cancel_order_is_noop_true():
    gw = ReplayGateway()
    ref = await gw.qualify_equity("AAPL")
    order = PreparedOrder(action="BUY", quantity=1, order_type="LMT",
                          limit_price=10.0, tif="DAY")
    trade = await gw.place_order(ref, order, "cid")
    assert await gw.cancel_order(trade) is True
