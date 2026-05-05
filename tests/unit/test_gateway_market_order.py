import pytest
from unittest.mock import MagicMock, AsyncMock
from infra.ib.gateway import IBGateway
from infra.ib.models import PreparedOrder, BrokerContractRef


@pytest.fixture
def gw_with_mock_ib():
    gw = IBGateway.__new__(IBGateway)
    gw._ib = MagicMock()
    gw._ib.qualifyContractsAsync = AsyncMock(return_value=[MagicMock()])
    gw._ib.placeOrder = MagicMock(return_value=MagicMock())
    gw._read_breaker = MagicMock()
    gw._write_breaker = MagicMock()
    gw._policy = MagicMock(ib_gateway=MagicMock(mode="paper",
                                                  port=7497,
                                                  paper_account_prefixes=["DU"]))
    gw._account_id = "DUQ123"
    return gw


@pytest.mark.asyncio
async def test_place_order_market_uses_market_order(gw_with_mock_ib):
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    order = PreparedOrder(action="BUY", quantity=10, order_type="MKT",
                          limit_price=None, tif="DAY")
    await gw_with_mock_ib.place_order(contract, order, "client-1")
    placed = gw_with_mock_ib._ib.placeOrder.call_args[0][1]
    assert type(placed).__name__ == "MarketOrder"


@pytest.mark.asyncio
async def test_place_order_limit_unchanged(gw_with_mock_ib):
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    order = PreparedOrder(action="BUY", quantity=10, order_type="LMT",
                          limit_price=150.0, tif="DAY")
    await gw_with_mock_ib.place_order(contract, order, "client-2")
    placed = gw_with_mock_ib._ib.placeOrder.call_args[0][1]
    assert type(placed).__name__ == "LimitOrder"
    assert placed.lmtPrice == 150.0


@pytest.mark.asyncio
async def test_place_order_lmt_with_no_limit_price_raises(gw_with_mock_ib):
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    order = PreparedOrder(action="BUY", quantity=10, order_type="LMT",
                          limit_price=None, tif="DAY")
    with pytest.raises(ValueError):
        await gw_with_mock_ib.place_order(contract, order, "client-3")
