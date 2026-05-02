from __future__ import annotations
from datetime import datetime, timezone


class TraderStateStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def get_unavailable_until(self, handle: str) -> datetime | None:
        cursor = await self._conn.execute(
            "SELECT unavailable_until FROM trader_state WHERE trader_handle = ?",
            (handle,),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])

    async def set_unavailable_until(self, *, handle: str, until: datetime) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO trader_state (trader_handle, unavailable_until, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(trader_handle) DO UPDATE SET
                 unavailable_until=excluded.unavailable_until,
                 updated_at=excluded.updated_at""",
            (handle, until.isoformat(), now),
        )
        await self._conn.commit()
