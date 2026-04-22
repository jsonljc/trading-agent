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
        try:
            open_orders = await self._gateway.get_open_orders()
        except IBGatewayUnavailable:
            logger.warning("ExecutionReconciler: gateway unavailable, skipping remaining rows this cycle")
            return
        open_order_ids = {str(o.orderId) for o in open_orders}
        for row in rows:
            broker_order_id = row["broker_order_id"]
            if not broker_order_id:
                continue
            if broker_order_id not in open_order_ids:
                logger.warning(
                    "ExecutionReconciler: order %s not in open orders — marked for manual review",
                    broker_order_id,
                )
