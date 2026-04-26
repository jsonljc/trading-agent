from __future__ import annotations
import asyncio
import logging
from infra.ib.models import FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class ExecutionReconciler:
    def __init__(self, gateway, execution_store, trade_intent_store=None,
                 interval_seconds: int = 60) -> None:
        self._gateway = gateway
        self._store = execution_store
        self._intent_store = trade_intent_store
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
        await self._reconcile_executions()
        await self._reconcile_intents()

    async def _reconcile_executions(self) -> None:
        rows = await self._store.get_uncertain_executions()
        if not rows:
            return
        logger.info("ExecutionReconciler: %d uncertain executions", len(rows))
        try:
            open_orders = await self._gateway.get_open_orders()
        except IBGatewayUnavailable:
            logger.warning("ExecutionReconciler: gateway unavailable, skipping this cycle")
            return
        open_order_ids = {str(o.orderId) for o in open_orders}
        for row in rows:
            broker_order_id = row["broker_order_id"]
            if not broker_order_id:
                continue
            if broker_order_id not in open_order_ids:
                logger.warning(
                    "ExecutionReconciler: order %s not in open orders — manual review needed",
                    broker_order_id,
                )

    async def _reconcile_intents(self) -> None:
        if self._intent_store is None:
            return
        rows = await self._intent_store.get_pending_outbox()
        if not rows:
            return
        logger.warning(
            "ExecutionReconciler: %d intent(s) stuck in pending/dispatched outbox",
            len(rows),
        )
        for row in rows:
            logger.warning(
                "ExecutionReconciler: intent_id=%s outbox_status=%s — manual review needed",
                row["intent_id"],
                row["outbox_status"],
            )
