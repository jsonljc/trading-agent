"""CapturingTraceStore — an in-memory trace_store for the replay harness.

Implements the trace_store interface the Orchestrator drives
(start / record_skill / finish) but, instead of writing to the DB, records per
trace the ordered (skill_name, status) path, the accumulated updates, and the
terminal status/reason so the runner can read the decision path.
"""
from __future__ import annotations


class CapturingTraceStore:
    def __init__(self) -> None:
        # trace_id -> {event_id, path, updates, status, reason}
        self.records: dict[str, dict] = {}

    async def start(self, trace_id: str, event_id: str) -> None:
        self.records[trace_id] = {
            "event_id": event_id,
            "path": [],
            "updates": {},
            "status": "running",
            "reason": None,
        }

    async def record_skill(self, trace_id: str, skill_name: str,
                           status: str, updates: dict) -> None:
        rec = self.records.setdefault(
            trace_id,
            {"event_id": None, "path": [], "updates": {},
             "status": "running", "reason": None},
        )
        rec["path"].append((skill_name, status))
        if updates:
            rec["updates"].update(updates)

    async def finish(self, trace_id: str, status: str,
                     reason: str | None = None) -> None:
        rec = self.records.setdefault(
            trace_id,
            {"event_id": None, "path": [], "updates": {},
             "status": "running", "reason": None},
        )
        rec["status"] = status
        rec["reason"] = reason
