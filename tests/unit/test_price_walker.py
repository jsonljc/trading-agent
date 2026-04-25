import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.price_walker import PriceWalker
from infra.ib.models import BrokerContractRef, FillStatus


def _policy(walk_profile="aggressive_fast", max_chase_pct=0.15, reprice_interval_ms=2500):
    p = MagicMock()
    p.execution.walk_profile = walk_profile
    p.execution.walk_profiles = {
        "aggressive_fast": [0.01, 0.03, 0.06, 0.10],
        "cautious_fast":   [0.00, 0.02, 0.05, 0.10],
    }
    p.execution.max_chase_pct = max_chase_pct
    p.execution.reprice_interval_ms = reprice_interval_ms
    p.ib_gateway.mode = "paper"
    p.ib_gateway.port = 4002
    p.ib_gateway.paper_account_prefixes = ["DU"]
    return p


def _contract():
    return BrokerContractRef(
        symbol="NVDA", sec_type="OPT", exchange="SMART", currency="USD",
        expiry="20261218", strike=150.0, right="C", qualified=True
    )


def _gateway(ask=5.50, fill=True):
    fake_trade = MagicMock()
    fake_trade.order.orderId = "IB-1"
    fake_trade.order.permId = 42
    fill_status = MagicMock()
    fill_status.status = "Filled" if fill else "Submitted"
    fill_status.filled = 9
    fill_status.remaining = 0
    fill_status.avgFillPrice = 5.56
    fake_trade.orderStatus = fill_status

    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.cancel_order = AsyncMock(return_value=True)
    gw.get_option_ask = AsyncMock(return_value=(ask, 0.0))
    gw._account_id = "DU12345"
    return gw, fake_trade


def _store():
    s = MagicMock()
    s.update_execution_state = AsyncMock()
    s.update_outbox_status = AsyncMock()
    return s


def _ctx(ticker="NVDA", quantity=9, limit_price=5.56, initial_reference_ask=5.50,
         execution_mode="auto_live", intent_id="evt1:NVDA:long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({
        "ticker": ticker,
        "quantity": quantity,
        "limit_price": limit_price,
        "initial_reference_ask": initial_reference_ask,
        "selected_contract": _contract(),
        "execution_mode": execution_mode,
        "intent_id": intent_id,
        "signal_id": "sig1",
        "action": "BUY",
    })
    return ctx


async def test_fills_on_first_step():
    """Happy path: first order fills immediately."""
    gw, trade = _gateway(ask=5.50, fill=True)
    trade.orderStatus.status = "Filled"

    store = _store()
    skill = PriceWalker(_policy(), gw, store)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["fill_status"] == "filled"
    assert result.updates["order_attempt_count"] == 1
    gw.place_order.assert_called_once()
    gw.cancel_order.assert_not_called()


async def test_walk_exhausted_after_all_steps():
    """All steps run without fill → cancelled_unfilled / walk_exhausted."""
    gw, trade = _gateway(ask=5.50, fill=False)
    trade.orderStatus.status = "Submitted"

    gw.cancel_order = AsyncMock(side_effect=lambda t, **kw: setattr(
        t.orderStatus, 'status', 'Cancelled') or True
    )

    store = _store()
    skill = PriceWalker(_policy(reprice_interval_ms=50), gw, store)
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "walk_exhausted" in result.reason or "cancelled_unfilled" in result.reason


async def test_price_exceeded_cap_stops_walk_early():
    """If the next step price would exceed max_chase_price, stop before placing."""
    gw, trade = _gateway(ask=5.50, fill=False)
    trade.orderStatus.status = "Submitted"
    gw.cancel_order = AsyncMock(side_effect=lambda t, **kw: setattr(
        t.orderStatus, 'status', 'Cancelled') or True
    )

    store = _store()
    skill = PriceWalker(_policy(max_chase_pct=0.05, reprice_interval_ms=50), gw, store)
    ctx = _ctx(initial_reference_ask=5.00)
    result = await skill.run(ctx)
    assert result.status == "skip"
    assert "price_exceeded_cap" in result.reason or "cancelled_unfilled" in result.reason


async def test_stale_quote_terminates_walk():
    """Quote age > 5s terminates walk immediately."""
    gw, trade = _gateway(ask=5.50, fill=False)
    gw.get_option_ask = AsyncMock(return_value=(5.50, 6.0))  # 6 seconds old → stale

    store = _store()
    skill = PriceWalker(_policy(reprice_interval_ms=50), gw, store)
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "stale_quote" in result.reason
