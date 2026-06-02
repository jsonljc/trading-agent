import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.execution_reconciler import ExecutionReconciler


def _reconciler(pending_rows=None, uncertain_rows=None, open_orders=None):
    gateway = MagicMock()
    gateway.get_open_orders = AsyncMock(return_value=open_orders or [])

    exec_store = MagicMock()
    exec_store.get_uncertain_executions = AsyncMock(return_value=uncertain_rows or [])

    intent_store = MagicMock()
    intent_store.get_pending_outbox = AsyncMock(return_value=pending_rows or [])

    return ExecutionReconciler(gateway, exec_store, intent_store, interval_seconds=60)


async def test_reconcile_scans_pending_outbox():
    row = MagicMock()
    row.__getitem__ = lambda self, key: "evt1:NVDA:long" if key == "intent_id" else "pending"
    reconciler = _reconciler(pending_rows=[row])
    await reconciler._reconcile_intents()
    reconciler._intent_store.get_pending_outbox.assert_called_once()


async def test_reconcile_no_pending_no_error():
    reconciler = _reconciler()
    await reconciler._reconcile_intents()
    reconciler._intent_store.get_pending_outbox.assert_called_once()


def _order(order_id, order_ref=""):
    o = MagicMock()
    o.orderId = order_id
    o.orderRef = order_ref
    return o


def _position(symbol, qty):
    p = MagicMock()
    p.contract.symbol = symbol
    p.position = qty
    return p


def _build(pending_rows, open_orders=None, positions=None):
    gateway = MagicMock()
    gateway.get_open_orders = AsyncMock(return_value=open_orders or [])
    gateway.get_positions = AsyncMock(return_value=positions or [])
    intent_store = MagicMock()
    intent_store.get_pending_outbox = AsyncMock(return_value=pending_rows)
    return ExecutionReconciler(gateway, MagicMock(), intent_store, interval_seconds=60)


async def test_flags_vanished_dispatched_order():
    # A 'dispatched' intent whose broker order is no longer working at IB and
    # has no live position == something happened while we were down.
    row = {"intent_id": "e1:NVDA:long", "broker_order_ref": "11",
           "ticker": "NVDA", "outbox_status": "dispatched"}
    rec = _build([row], open_orders=[], positions=[])
    summary = await rec.reconcile_once()
    assert len(summary["vanished"]) == 1
    assert summary["vanished"][0]["intent_id"] == "e1:NVDA:long"
    assert summary["vanished"][0]["in_position"] is False


async def test_vanished_order_with_live_position_is_flagged_filled_while_down():
    row = {"intent_id": "e1:NVDA:long", "broker_order_ref": "11",
           "ticker": "NVDA", "outbox_status": "dispatched"}
    rec = _build([row], open_orders=[], positions=[_position("NVDA", 100)])
    summary = await rec.reconcile_once()
    assert summary["vanished"][0]["in_position"] is True


async def test_working_order_not_flagged():
    row = {"intent_id": "e1:NVDA:long", "broker_order_ref": "11",
           "ticker": "NVDA", "outbox_status": "dispatched"}
    rec = _build([row], open_orders=[_order(11)])  # str(11) == "11" matches ref
    summary = await rec.reconcile_once()
    assert summary["vanished"] == []


async def test_pending_without_broker_ref_not_flagged():
    # 'pending' (never submitted) has no broker_order_ref -> not a vanished order.
    row = {"intent_id": "e1:NVDA:long", "broker_order_ref": None,
           "ticker": "NVDA", "outbox_status": "pending"}
    rec = _build([row], open_orders=[])
    summary = await rec.reconcile_once()
    assert summary["vanished"] == []


async def test_flags_orphan_broker_order():
    # A live IB order that looks like ours but maps to no in-flight intent.
    rec = _build([], open_orders=[_order(99, order_ref="trace:shares:e9")])
    summary = await rec.reconcile_once()
    assert len(summary["orphans"]) == 1
    assert summary["orphans"][0]["order_id"] == "99"


async def test_discrepancy_callback_fired():
    fired = []
    row = {"intent_id": "e1:NVDA:long", "broker_order_ref": "11",
           "ticker": "NVDA", "outbox_status": "dispatched"}
    rec = _build([row], open_orders=[])
    rec._on_discrepancy = AsyncMock(side_effect=lambda s: fired.append(s))
    await rec.reconcile_once()
    assert len(fired) == 1
