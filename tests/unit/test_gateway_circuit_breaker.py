import pytest
from unittest.mock import AsyncMock, patch
from infra.ib.gateway import IBGateway, IBGatewayUnavailable, LiveTradingBlocked
from infra.ib.models import BrokerContractRef, PreparedOrder


def _paper_policy():
    from unittest.mock import MagicMock
    p = MagicMock()
    p.ib_gateway.host = "127.0.0.1"
    p.ib_gateway.port = 7497
    p.ib_gateway.client_id = 1
    p.ib_gateway.mode = "paper"
    p.ib_gateway.paper_account_prefixes = ["DU"]
    return p


@pytest.mark.asyncio
async def test_read_breaker_opens_after_three_failures():
    gw = IBGateway(_paper_policy())
    gw._ib = AsyncMock()
    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=ConnectionError("refused"))
    # Simulate 3 consecutive read failures
    for _ in range(3):
        gw._read_breaker._record_failure()
    assert gw._read_breaker.is_open()


@pytest.mark.asyncio
async def test_read_breaker_closed_after_success():
    gw = IBGateway(_paper_policy())
    for _ in range(3):
        gw._read_breaker._record_failure()
    gw._read_breaker._record_success()
    assert not gw._read_breaker.is_open()


@pytest.mark.asyncio
async def test_fill_timeout_does_not_trip_write_breaker():
    gw = IBGateway(_paper_policy())
    # Fill timeout must never trip breakers
    initial_read = gw._read_breaker._failure_count
    initial_write = gw._write_breaker._failure_count
    # Simulate fill timeout recording (should be a no-op on breakers)
    gw._record_fill_timeout()
    assert gw._read_breaker._failure_count == initial_read
    assert gw._write_breaker._failure_count == initial_write


@pytest.mark.asyncio
async def test_live_trading_blocked_when_mode_is_paper():
    gw = IBGateway(_paper_policy())
    gw._connected = True
    gw._account_id = "DU123456"
    contract = BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", qualified=True,
    )
    order = PreparedOrder(action="BUY", quantity=1, order_type="LMT", limit_price=150.0, tif="DAY")
    # Test that wrong port raises LiveTradingBlocked
    gw._policy.ib_gateway.port = 7496
    with pytest.raises(LiveTradingBlocked):
        await gw.place_order(contract, order, "test-key")
