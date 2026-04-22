from __future__ import annotations
import asyncio
import logging
from infra.ib.models import FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class ExecutionReconciler:
    def __init__(self, gateway, execution_store, interval_seconds: int = 60) -> None:
        self._gateway = gateway
        self._store = execution_store
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._loop())
        logger.info("ExecutionReconciler started (interval=%ds)", self._interval)

    async def _loop(self) -> None:
        while True:
            try:
                await self._reconcile()
            except Exception as exc:
                logger.exception("ExecutionReconciler error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _reconcile(self) -> None:
        rows = await self._store.get_uncertain_executions()
        if not rows:
            return
        logger.info("ExecutionReconciler: %d uncertain executions to reconcile", len(rows))
        for row in rows:
            broker_order_id = row["broker_order_id"]
            if not broker_order_id:
                continue
            try:
                open_orders = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._gateway._ib.openOrders() if self._gateway._ib else [],
                )
                matched = [o for o in open_orders if str(o.orderId) == broker_order_id]
                if not matched:
                    logger.warning(
                        "ExecutionReconciler: order %s not in open orders — marking timed_out_pending for manual review",
                        broker_order_id,
                    )
                    continue
            except IBGatewayUnavailable:
                logger.warning("ExecutionReconciler: gateway unavailable, skipping reconcile")
                return
