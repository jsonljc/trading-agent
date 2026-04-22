from __future__ import annotations
from datetime import datetime, timezone
import aiosqlite
from infra.ib.models import FillStatus


class ExecutionStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert_execution(self, record: dict) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO executions
               (id, signal_id, trace_id, instrument_type, ticker,
                contract_ref_json, quantity, notional_estimate, limit_price,
                sizing_reason, capped_by, broker_order_id, perm_id, status,
                filled_qty, avg_fill_price, idempotency_key,
                submitted_at, filled_at, last_known_at)
               VALUES
               (:id, :signal_id, :trace_id, :instrument_type, :ticker,
                :contract_ref_json, :quantity, :notional_estimate, :limit_price,
                :sizing_reason, :capped_by, :broker_order_id, :perm_id, :status,
                :filled_qty, :avg_fill_price, :idempotency_key,
                :submitted_at, :filled_at, :last_known_at)""",
            record,
        )
        await self._conn.commit()

    async def update_execution_status(
        self,
        execution_id: str,
        status: FillStatus,
        filled_qty: int = 0,
        avg_fill_price: float | None = None,
        broker_order_id: str | None = None,
        perm_id: int | None = None,
        filled_at: str | None = None,
    ) -> None:
        await self._conn.execute(
            """UPDATE executions SET
               status=:status, filled_qty=:filled_qty,
               avg_fill_price=:avg_fill_price, broker_order_id=:broker_order_id,
               perm_id=:perm_id, filled_at=:filled_at,
               last_known_at=:last_known_at
               WHERE id=:id""",
            {
                "status": status.value,
                "filled_qty": filled_qty,
                "avg_fill_price": avg_fill_price,
                "broker_order_id": broker_order_id,
                "perm_id": perm_id,
                "filled_at": filled_at,
                "last_known_at": datetime.now(timezone.utc).isoformat(),
                "id": execution_id,
            },
        )
        await self._conn.commit()

    async def insert_audit_log(self, record: dict) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO execution_audit_log
               (id, execution_id, signal_id, trace_id,
                ctx_snapshot_json, pipeline_outcome, created_at)
               VALUES
               (:id, :execution_id, :signal_id, :trace_id,
                :ctx_snapshot_json, :pipeline_outcome, :created_at)""",
            record,
        )
        await self._conn.commit()

    async def get_uncertain_executions(self) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            """SELECT * FROM executions
               WHERE status IN (?, ?)""",
            (FillStatus.SUBMITTED_UNFILLED.value, FillStatus.TIMED_OUT_PENDING.value),
        ) as cur:
            return await cur.fetchall()
