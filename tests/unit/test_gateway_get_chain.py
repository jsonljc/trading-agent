import pytest
from unittest.mock import AsyncMock, MagicMock
from infra.ib.gateway import IBGateway
from infra.ib.models import BrokerContractRef


def _policy(min_expiry_days=180):
    p = MagicMock()
    p.ib_gateway.host = "127.0.0.1"
    p.ib_gateway.port = 4002
    p.ib_gateway.client_id = 1
    p.ib_gateway.mode = "paper"
    p.ib_gateway.paper_account_prefixes = ["DU"]
    p.instrument_policy.min_expiry_days = min_expiry_days
    return p


def _make_chain(strikes, expirations):
    chain = MagicMock()
    chain.strikes = strikes
    chain.expirations = expirations
    return chain


async def test_get_chain_pre_filters_to_calls_near_spot():
    from datetime import date, timedelta
    today = date.today()
    near_expiry = (today + timedelta(days=30)).strftime("%Y%m%d")   # filtered out (< min_expiry_days)
    far_expiry  = (today + timedelta(days=200)).strftime("%Y%m%d")  # kept

    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()

    stock_ref = MagicMock()
    stock_ref.conId = 12345

    chain = _make_chain(
        strikes=[140.0, 145.0, 148.0, 150.0, 152.0, 155.0, 160.0, 165.0],
        expirations=[near_expiry, far_expiry],
    )
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    qualify_calls = []
    async def fake_qualify(contract):
        qualify_calls.append(contract)
        # First call is for stock qualification (secType='STK', strike=0.0)
        if not hasattr(contract, 'secType') or contract.secType != "OPT":
            return [stock_ref]
        c = MagicMock()
        c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike; c.right = contract.right
        c.multiplier = "100"; c.localSymbol = None; c.tradingClass = None
        return [c]

    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=fake_qualify)

    ticker_mock = MagicMock()
    ticker_mock.bid = 2.0
    ticker_mock.ask = 2.5
    gw._ib.reqTickersAsync = AsyncMock(return_value=[ticker_mock])

    spot = 152.0
    candidates = await gw.get_chain("NVDA", spot_price=spot)

    for c in candidates:
        assert c.right == "C"
        assert c.expiry == f"{far_expiry[:4]}-{far_expiry[4:6]}-{far_expiry[6:]}"
    strikes = {c.strike for c in candidates}
    assert 148.0 in strikes or 150.0 in strikes
    assert 165.0 not in strikes


async def test_get_chain_spread_is_mid_based_and_populates_oi_volume():
    from datetime import date, timedelta
    far_expiry = (date.today() + timedelta(days=200)).strftime("%Y%m%d")

    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()
    stock_ref = MagicMock(); stock_ref.conId = 12345

    chain = _make_chain(strikes=[150.0], expirations=[far_expiry])
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    async def fake_qualify(contract):
        if not hasattr(contract, 'secType') or contract.secType != "OPT":
            return [stock_ref]
        c = MagicMock()
        c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike; c.right = contract.right
        c.multiplier = "100"; c.localSymbol = None; c.tradingClass = None
        return [c]
    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=fake_qualify)

    td = MagicMock()
    td.bid = 2.0; td.ask = 3.0        # mid 2.5 -> spread_pct = 1.0/2.5 = 0.40
    td.callOpenInterest = 500; td.volume = 300
    gw._ib.reqTickersAsync = AsyncMock(return_value=[td])

    candidates = await gw.get_chain("NVDA", spot_price=150.0)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.spread_pct == pytest.approx(0.40)   # (ask-bid)/MID, not /ask (0.333)
    assert c.open_interest == 500
    assert c.volume == 300


async def test_get_chain_oi_volume_none_when_unavailable():
    # Delayed data: ticker has no OI/volume fields (nan/missing) -> None, not a crash.
    from datetime import date, timedelta
    far_expiry = (date.today() + timedelta(days=200)).strftime("%Y%m%d")
    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()
    stock_ref = MagicMock(); stock_ref.conId = 12345
    chain = _make_chain(strikes=[150.0], expirations=[far_expiry])
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    async def fake_qualify(contract):
        if not hasattr(contract, 'secType') or contract.secType != "OPT":
            return [stock_ref]
        c = MagicMock()
        c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike; c.right = contract.right
        c.multiplier = "100"; c.localSymbol = None; c.tradingClass = None
        return [c]
    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=fake_qualify)

    td = MagicMock()
    td.bid = 2.0; td.ask = 2.5
    td.callOpenInterest = float("nan"); td.volume = -1
    gw._ib.reqTickersAsync = AsyncMock(return_value=[td])

    candidates = await gw.get_chain("NVDA", spot_price=150.0)
    assert len(candidates) == 1
    assert candidates[0].open_interest is None
    assert candidates[0].volume is None


async def test_get_chain_partial_qualify_failures_skipped():
    from datetime import date, timedelta
    far_expiry = (date.today() + timedelta(days=200)).strftime("%Y%m%d")

    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()

    stock_ref = MagicMock()
    stock_ref.conId = 12345

    chain = _make_chain(strikes=[148.0, 150.0, 152.0, 155.0, 160.0], expirations=[far_expiry])
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    call_count = [0]
    async def flaky_qualify(contract):
        if not hasattr(contract, 'secType') or contract.secType != "OPT":
            return [stock_ref]
        call_count[0] += 1
        if call_count[0] == 1:
            return []  # first option call fails
        c = MagicMock()
        c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike; c.right = contract.right
        c.multiplier = "100"; c.localSymbol = None; c.tradingClass = None
        return [c]

    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=flaky_qualify)
    td = MagicMock(); td.bid = 2.0; td.ask = 2.5
    gw._ib.reqTickersAsync = AsyncMock(return_value=[td])

    candidates = await gw.get_chain("NVDA", spot_price=152.0)
    assert len(candidates) >= 1


async def test_get_chain_returns_single_candidate():
    from datetime import date, timedelta
    far_expiry = (date.today() + timedelta(days=200)).strftime("%Y%m%d")

    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()
    stock_ref = MagicMock(); stock_ref.conId = 12345

    chain = _make_chain(strikes=[150.0, 152.0, 155.0, 160.0], expirations=[far_expiry])
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    opt_calls = [0]
    async def one_survivor(contract):
        if not hasattr(contract, 'secType') or contract.secType != "OPT":
            return [stock_ref]
        opt_calls[0] += 1
        if opt_calls[0] >= 2:
            return []  # only the first option qualifies
        c = MagicMock()
        c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike; c.right = contract.right
        c.multiplier = "100"; c.localSymbol = None; c.tradingClass = None
        return [c]

    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=one_survivor)
    td = MagicMock(); td.bid = 2.0; td.ask = 2.5
    gw._ib.reqTickersAsync = AsyncMock(return_value=[td])

    candidates = await gw.get_chain("NVDA", spot_price=152.0)
    assert len(candidates) == 1
