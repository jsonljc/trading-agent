import json
from datetime import datetime, timezone
import aiosqlite


class TraceStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def start(self, trace_id: str, event_id: str) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO work_traces (trace_id, event_id, status, started_at) VALUES (?, ?, 'running', ?)",
            (trace_id, event_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def finish(self, trace_id: str, status: str, failure_reason: str | None = None) -> None:
        await self._conn.execute(
            "UPDATE work_traces SET status=?, finished_at=?, failure_reason=? WHERE trace_id=?",
            (status, datetime.now(timezone.utc).isoformat(), failure_reason, trace_id),
        )
        await self._conn.commit()

    async def record_skill(self, trace_id: str, skill_name: str, status: str, output: dict) -> None:
        await self._conn.execute(
            "INSERT INTO skill_outputs (trace_id, skill_name, status, output_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (trace_id, skill_name, status, json.dumps(output), datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()
