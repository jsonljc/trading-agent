import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.order_submitter import OrderSubmitter
from infra.ib.models import BrokerContractRef, FillStatus
from infra.ib.gateway import IBGatewayUnavailable


def _ref(sec_type="OPT", qualified=True):
    return BrokerContractRef(symbol="AAPL", sec_type=sec_type, exchange="SMART",
                              currency="USD", qualified=qualified)


def _ctx(instrument_type="option", quantity=2, limit_price=5.25, ref=None):
    c = Context(trace_id="trace-1", event_id="sig-1")
    c.update({
        "signal_id": "sig-1",
        "instrument_type": instrument_type,
        "ticker": "AAPL",
        "selected_contract": ref or _ref(sec_type="OPT" if instrument_type=="option" else "STK"),
        "quantity": quantity,
        "limit_price": limit_price,
        "notional_estimate": quantity * limit_price * 100,
        "sizing_reason": "high_conviction",
        "capped_by": None,
    })
    return c


def _gateway(trade_id="IB-1"):
    fake_trade = MagicMock()
    fake_trade.order.orderId = trade_id
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.qualify = AsyncMock(side_effect=lambda ref: setattr(ref, 'qualified', True) or ref)
    return gw


@pytest.mark.asyncio
async def test_option_submitter_writes_execution_row(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = _gateway()
    skill = OrderSubmitter(_gateway(), store)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["broker_order_id"] == "IB-1"
    assert "execution_id" in result.updates
    idempotency_key = result.updates["idempotency_key"]
    assert idempotency_key == "trace-1:OrderSubmitter:sig-1"


@pytest.mark.asyncio
async def test_equity_submitter_qualifies_before_submit(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = _gateway()
    ref = _ref(sec_type="STK", qualified=False)
    result = await OrderSubmitter(gw, store).run(_ctx(instrument_type="equity", ref=ref))
    assert result.status == "success"
    gw.qualify.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_unavailable_fails(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.place_order = AsyncMock(side_effect=IBGatewayUnavailable("write breaker open"))
    gw.qualify = AsyncMock(side_effect=lambda ref: ref)
    result = await OrderSubmitter(gw, store).run(_ctx())
    assert result.status == "fail"
    assert "broker_unavailable" in result.reason


@pytest.mark.asyncio
async def test_execution_row_written_before_place_order(db):
    from infra.storage.execution_store import ExecutionStore
    call_order = []
    store = ExecutionStore(db)
    original_insert = store.insert_execution
    async def tracked_insert(record):
        call_order.append("insert")
        return await original_insert(record)
    store.insert_execution = tracked_insert

    gw = MagicMock()
    async def tracked_place(contract_ref, order, client_order_id):
        call_order.append("place_order")
        fake = MagicMock()
        fake.order.orderId = "IB-X"
        return fake
    gw.place_order = tracked_place
    gw.qualify = AsyncMock(side_effect=lambda ref: ref)

    await OrderSubmitter(gw, store).run(_ctx())
    assert call_order == ["insert", "place_order"]
