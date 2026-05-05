from __future__ import annotations
import asyncio
import logging

logger = logging.getLogger(__name__)


class ExecutionReconciler:
    """Background task that flags trade_intents stuck in pending/dispatched
    outbox state for manual review.

    Note: the executions table is no longer written by the live MKT chain
    (OrderSubmitter, which wrote it, was removed); the executions-side
    reconciler half was dropped along with this cleanup.
    """

    def __init__(self, gateway, execution_store, trade_intent_store=None,
                 interval_seconds: int = 60) -> None:
        self._gateway = gateway  # retained for constructor compat
        self._intent_store = trade_intent_store
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("ExecutionReconciler started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._reconcile_intents()
            except Exception as exc:
                logger.exception("ExecutionReconciler error: %s", exc)
            await asyncio.sleep(self._interval)

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
