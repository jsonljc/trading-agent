from __future__ import annotations
from datetime import datetime, timezone


class ExamplesPendingStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def insert(self, *, trader_handle: str, msg_text: str,
                     proposed_bucket: str, proposed_why: str, source: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._conn.execute(
            """INSERT INTO trader_examples_pending
               (trader_handle, msg_text, proposed_bucket, proposed_why, source, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (trader_handle, msg_text, proposed_bucket, proposed_why, source, now),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def list_pending(self, *, trader_handle: str | None = None) -> list[dict]:
        if trader_handle:
            cursor = await self._conn.execute(
                "SELECT * FROM trader_examples_pending WHERE status='pending' AND trader_handle=? ORDER BY id",
                (trader_handle,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM trader_examples_pending WHERE status='pending' ORDER BY id"
            )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_resolved(self, *, trader_handle: str) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM trader_examples_pending WHERE status!='pending' AND trader_handle=? ORDER BY id",
            (trader_handle,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def resolve(self, pending_id: int, *, status: str, resolved_bucket: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """UPDATE trader_examples_pending
               SET status=?, resolved_bucket=?, resolved_at=?
               WHERE id=?""",
            (status, resolved_bucket, now, pending_id),
        )
        await self._conn.commit()
