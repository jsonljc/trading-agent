from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone


class ClassificationLogStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def insert(self, *, event_id: str, trader_handle: str, msg_text: str,
                     features: dict, llm_response: dict | None, bucket: str,
                     confidence: float, size_pct: float, size_source: str,
                     action_taken: str, reason: str,
                     ticker: str | None = None, side: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO classification_log
               (event_id, trader_handle, msg_text, features_json, llm_response_json,
                bucket, confidence, size_pct, size_source, action_taken, reason,
                ticker, side, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, trader_handle, msg_text, json.dumps(features),
             json.dumps(llm_response) if llm_response is not None else None,
             bucket, confidence, size_pct, size_source, action_taken, reason,
             ticker, side, now),
        )
        await self._conn.commit()

    async def has_fired_recently(self, *, trader_handle: str, ticker: str,
                                 side: str, hours: float,
                                 exclude_event_id: str | None = None) -> bool:
        """True if a 'fired' classification for (trader, ticker, side) exists
        within the last `hours`, EXCLUDING `exclude_event_id`.

        The exclusion is essential: SameDayDedupGate runs AFTER
        ClassificationLogger has already committed the current event's own
        'fired' row, so without excluding it the gate would match that row and
        suppress the very first signal (the fix/no-trades-may-8 regression)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        sql = ["""SELECT 1 FROM classification_log
                  WHERE trader_handle = ?
                    AND ticker = ?
                    AND side = ?
                    AND action_taken = 'fired'
                    AND created_at >= ?"""]
        params = [trader_handle, ticker, side, cutoff]
        if exclude_event_id is not None:
            sql.append("AND event_id != ?")
            params.append(exclude_event_id)
        sql.append("LIMIT 1")
        cursor = await self._conn.execute("\n".join(sql), params)
        return (await cursor.fetchone()) is not None

    async def recent_for_trader(self, trader_handle: str, *, limit: int = 100) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM classification_log
               WHERE trader_handle = ?
               ORDER BY id DESC LIMIT ?""",
            (trader_handle, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
