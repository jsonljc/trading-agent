from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import FillStatus

logger = logging.getLogger(__name__)


class FillWaiter(Skill):
    name = "FillWaiter"

    def __init__(self, gateway, execution_store, timeout: float | None = None) -> None:
        self._gateway = gateway
        self._store = execution_store
        self._timeout = timeout  # None → use policy value set at runtime

    async def run(self, ctx: Context) -> SkillResult:
        trade = ctx.get("_trade")
        execution_id = ctx.get("execution_id")
        timeout = self._timeout if self._timeout is not None else 30.0

        fill = await self._gateway.wait_fill(trade, timeout=timeout)

        await self._store.update_execution_status(
            execution_id=execution_id,
            status=fill.status,
            filled_qty=fill.filled_qty,
            avg_fill_price=fill.avg_fill_price,
            broker_order_id=fill.broker_order_id,
            perm_id=fill.perm_id,
        )

        updates = {
            "fill_status": fill.status.value,
            "filled_qty": fill.filled_qty,
            "avg_fill_price": fill.avg_fill_price,
            "perm_id": fill.perm_id,
        }

        if fill.status == FillStatus.REJECTED:
            return SkillResult(
                status="fail",
                reason=f"fill rejected: broker_status={fill.last_status}",
                updates=updates,
            )

        if fill.status == FillStatus.CANCELLED:
            return SkillResult(
                status="fail",
                reason=f"fill cancelled: broker_status={fill.last_status}",
                updates=updates,
            )

        if fill.status == FillStatus.TIMED_OUT_PENDING:
            logger.warning(
                "FillWaiter: order %s timed out after %.0fs — ExecutionReconciler will resolve",
                fill.broker_order_id, timeout,
            )

        return SkillResult(status="success", updates=updates)
