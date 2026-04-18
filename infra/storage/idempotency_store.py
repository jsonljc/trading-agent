from datetime import datetime, timezone
import aiosqlite


class IdempotencyStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert_if_new(self, key: str, event_id: str, ticker: str, action: str) -> bool:
        """Insert key if not present. Returns True if inserted, False if already existed."""
        async with self._conn.execute(
            "INSERT OR IGNORE INTO idempotency_keys (key, event_id, ticker, action, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, event_id, ticker, action, datetime.now(timezone.utc).isoformat()),
        ) as cur:
            inserted = cur.rowcount > 0
        await self._conn.commit()
        return inserted
