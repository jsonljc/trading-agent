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
