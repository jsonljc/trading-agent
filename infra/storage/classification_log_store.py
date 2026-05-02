from __future__ import annotations
import json
from datetime import datetime, timezone


class ClassificationLogStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def insert(self, *, event_id: str, trader_handle: str, msg_text: str,
                     features: dict, llm_response: dict | None, bucket: str,
                     confidence: float, size_pct: float, size_source: str,
                     action_taken: str, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO classification_log
               (event_id, trader_handle, msg_text, features_json, llm_response_json,
                bucket, confidence, size_pct, size_source, action_taken, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, trader_handle, msg_text, json.dumps(features),
             json.dumps(llm_response) if llm_response is not None else None,
             bucket, confidence, size_pct, size_source, action_taken, reason, now),
        )
        await self._conn.commit()

    async def recent_for_trader(self, trader_handle: str, *, limit: int = 100) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM classification_log
               WHERE trader_handle = ?
               ORDER BY id DESC LIMIT ?""",
            (trader_handle, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
