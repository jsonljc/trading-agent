from datetime import datetime, timezone
import aiosqlite


class IdempotencyStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def exists(self, key: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM idempotency_keys WHERE key = ?", (key,)
        ) as cur:
            return await cur.fetchone() is not None

    async def insert(self, key: str, event_id: str, ticker: str, action: str) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO idempotency_keys (key, event_id, ticker, action, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, event_id, ticker, action, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()
