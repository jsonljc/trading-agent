from datetime import datetime, timezone
import aiosqlite


class SignalStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, event: dict) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO signal_events
               (id, source, channel, author, trigger_preview,
                full_message_text, capture_mode, message_fingerprint, received_at)
               VALUES (:id, :source, :channel, :author, :trigger_preview,
                       :full_message_text, :capture_mode, :message_fingerprint, :received_at)""",
            {**event, "received_at": event.get("received_at", datetime.now(timezone.utc).isoformat())},
        )
        await self._conn.commit()
