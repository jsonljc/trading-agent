from __future__ import annotations
from datetime import datetime, timezone
import aiosqlite


class TradeIntentStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, record: dict) -> None:
        cols = ", ".join(record.keys())
        placeholders = ", ".join(f":{k}" for k in record.keys())
        await self._conn.execute(
            f"INSERT OR IGNORE INTO trade_intents ({cols}) VALUES ({placeholders})",
            record,
        )
        await self._conn.commit()

    async def get(self, intent_id: str) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM trade_intents WHERE intent_id = ?", (intent_id,)
        ) as cur:
            return await cur.fetchone()

    async def update_policy_state(self, intent_id: str, policy_state: str) -> None:
        await self._conn.execute(
            "UPDATE trade_intents SET policy_state=?, updated_at=? WHERE intent_id=?",
            (policy_state, datetime.now(timezone.utc).isoformat(), intent_id),
        )
        await self._conn.commit()

    async def update_execution_state(
        self,
        intent_id: str,
        execution_state: str,
        fill_price: float | None = None,
        fill_qty: int | None = None,
        filled_at: str | None = None,
        cancelled_at: str | None = None,
        cancel_reason: str | None = None,
        dlq_reason: str | None = None,
        outbox_status: str | None = None,
        broker_order_ref: str | None = None,
        order_attempt_count: int | None = None,
        last_limit_price: float | None = None,
        order_submitted_at: str | None = None,
        order_ack_at: str | None = None,
        initial_reference_ask: float | None = None,
        initial_order_limit: float | None = None,
        max_chase_pct: float | None = None,
        max_chase_price: float | None = None,
        walk_profile: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        fields = {"execution_state": execution_state, "updated_at": now}
        if fill_price is not None:
            fields["fill_price"] = fill_price
        if fill_qty is not None:
            fields["fill_qty"] = fill_qty
        if filled_at is not None:
            fields["filled_at"] = filled_at
        if cancelled_at is not None:
            fields["cancelled_at"] = cancelled_at
        if cancel_reason is not None:
            fields["cancel_reason"] = cancel_reason
        if dlq_reason is not None:
            fields["dlq_reason"] = dlq_reason
        if outbox_status is not None:
            fields["outbox_status"] = outbox_status
        if broker_order_ref is not None:
            fields["broker_order_ref"] = broker_order_ref
        if order_attempt_count is not None:
            fields["order_attempt_count"] = order_attempt_count
        if last_limit_price is not None:
            fields["last_limit_price"] = last_limit_price
        if order_submitted_at is not None:
            fields["order_submitted_at"] = order_submitted_at
        if order_ack_at is not None:
            fields["order_ack_at"] = order_ack_at
        if initial_reference_ask is not None:
            fields["initial_reference_ask"] = initial_reference_ask
        if initial_order_limit is not None:
            fields["initial_order_limit"] = initial_order_limit
        if max_chase_pct is not None:
            fields["max_chase_pct"] = max_chase_pct
        if max_chase_price is not None:
            fields["max_chase_price"] = max_chase_price
        if walk_profile is not None:
            fields["walk_profile"] = walk_profile
        set_clause = ", ".join(f"{k}=:{k}" for k in fields)
        await self._conn.execute(
            f"UPDATE trade_intents SET {set_clause} WHERE intent_id=:_id",
            {**fields, "_id": intent_id},
        )
        await self._conn.commit()

    async def update_fill(
        self,
        intent_id: str,
        *,
        fill_price: float,
        fill_qty: int,
        execution_state: str = "filled",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE trade_intents SET fill_price=?, fill_qty=?, execution_state=?, "
            "updated_at=? WHERE intent_id=?",
            (fill_price, fill_qty, execution_state, now, intent_id),
        )
        await self._conn.commit()

    async def update_outbox_status(self, intent_id: str, outbox_status: str) -> None:
        await self._conn.execute(
            "UPDATE trade_intents SET outbox_status=?, updated_at=? WHERE intent_id=?",
            (outbox_status, datetime.now(timezone.utc).isoformat(), intent_id),
        )
        await self._conn.commit()

    async def get_filled_since(self, ticker: str, since: str) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            """SELECT * FROM trade_intents
               WHERE ticker=? AND execution_state='filled' AND filled_at >= ?""",
            (ticker, since),
        ) as cur:
            return await cur.fetchall()

    async def get_pending_outbox(self) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT * FROM trade_intents WHERE outbox_status IN ('pending', 'dispatched')"
        ) as cur:
            return await cur.fetchall()
