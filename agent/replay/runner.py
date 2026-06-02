"""Replay runner — drive a captured alert through the live skill chain.

For each alert we build a fresh in-memory DB (the live DB stays read-only), the
real stores + trader registry, a deterministic ReplayGateway and a recorded LLM,
then run the SAME phase1 + phase2b chains the agent runs in production via the
Orchestrator. Execution-eligibility is pinned to a fixed RTH clock so decisions
are deterministic regardless of the wall clock. No real orders are placed.
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.registry import build_phase1_chain, build_phase2b_execution_chain
from agent.replay.capture import CapturingTraceStore
from agent.replay.gateway import ReplayGateway
from agent.traders.profile import load_all_profiles
from agent.traders.registry import TraderRegistry
from infra.storage.db import SCHEMA
from infra.storage.idempotency_store import IdempotencyStore
from infra.storage.classification_log_store import ClassificationLogStore
from infra.storage.execution_store import ExecutionStore
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore

ET = ZoneInfo("America/New_York")
_FIXED_RTH = datetime(2026, 5, 15, 14, 30, tzinfo=ET)  # mid-RTH -> EXECUTE_NOW

_TRADERS_DIR = Path(__file__).resolve().parents[2] / "config" / "traders"

# Module-level cache: profiles never change during a replay run.
_REGISTRY: TraderRegistry | None = None


def _registry() -> TraderRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = TraderRegistry(load_all_profiles(_TRADERS_DIR))
    return _REGISTRY


class _NoopTelegram:
    async def send_message(self, text: str) -> None:
        return None


@dataclass
class ReplayResult:
    event_id: str
    channel: str
    message: str
    bucket: str | None
    action_taken: str | None
    side: str | None
    ticker: str | None
    final_status: str
    final_reason: str | None
    would_be_orders: list[dict] = field(default_factory=list)
    llm_recorded: bool = False


def _summarize_orders(placed: list[dict]) -> list[dict]:
    return [
        {
            "action": o["action"],
            "quantity": o["quantity"],
            "order_type": o["order_type"],
            "limit_price": o["limit_price"],
            "instrument": o["instrument"],
            "sec_type": o["sec_type"],
        }
        for o in placed
    ]


def _infer_action(bucket, updates) -> str | None:
    """Mirror ClassificationLogger._infer_action from ctx updates."""
    size_source = updates.get("size_source")
    if size_source == "drop_low_conf":
        return "dropped_low_conf"
    if size_source == "llm_error":
        return "llm_error"
    if size_source == "ticker_not_in_msg":
        return "ticker_not_in_msg"
    if bucket in (None, "SKIP"):
        return "skipped"
    return "fired"


async def replay_one(event_row, policy, recorded_llm, *,
                     net_liq: float = 100_000.0, quote: float = 100.0) -> ReplayResult:
    channel = event_row.get("channel") or ""
    author = event_row.get("author") or ""
    msg = event_row.get("full_message_text") or event_row.get("trigger_preview") or ""
    received_at = event_row.get("received_at")
    trigger_preview = event_row.get("trigger_preview") or msg
    event_id = event_row.get("id") or event_row.get("event_id")

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(SCHEMA)
        await conn.commit()

        idempotency_store = IdempotencyStore(conn)
        classification_log_store = ClassificationLogStore(conn)
        execution_store = ExecutionStore(conn)
        trade_intent_store = TradeIntentStore(conn)
        trim_store = TrimLadderStore(conn)
        gateway = ReplayGateway(quote=quote, net_liq=net_liq)
        telegram = _NoopTelegram()

        # Entry-only (no exits_store) — sells are out of scope for the harness.
        phase1 = build_phase1_chain(
            policy, idempotency_store, telegram,
            gateway=gateway,
            trader_registry=_registry(),
            classification_log_store=classification_log_store,
            llm_classifier=recorded_llm,
            trade_intent_store=trade_intent_store,
        )
        phase2b = build_phase2b_execution_chain(
            policy, execution_store, gateway, trade_intent_store,
            trim_store=trim_store,
        )

        # Pin execution eligibility to a fixed RTH clock so EXECUTE_NOW is
        # deterministic regardless of when the replay actually runs.
        from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
        for i, skill in enumerate(phase2b):
            if skill.name == "ExecutionEligibilityGuard":
                phase2b[i] = ExecutionEligibilityGuard(
                    policy, time_fn=lambda: _FIXED_RTH)
                break

        full_chain = phase1 + phase2b

        trace_id = str(uuid.uuid4())[:12]
        ctx = Context(trace_id=trace_id, event_id=event_id)
        ctx.update({
            "trigger_preview": trigger_preview,
            "full_message_text": msg,
            "channel": channel,
            "author": author,
            "received_at": received_at,
        })

        capture = CapturingTraceStore()
        # The Orchestrator only surfaces the terminating skip/fail reason via its
        # callbacks (finish() is called without a reason for skips), so capture it
        # here for the decision-path report.
        terminal = {"reason": None}

        async def _on_skip(ctx, reason):
            terminal["reason"] = reason

        async def _on_fail(ctx, reason):
            terminal["reason"] = reason

        orch = Orchestrator(full_chain, capture, on_skip=_on_skip, on_fail=_on_fail)
        # Measure whether THIS alert's classification actually replayed a recorded
        # LLM response. We can't key on the raw message text: MessageNormalizer
        # rewrites full_message_text (whitespace-collapsed) before the classifier
        # runs, and the recorded responses are keyed by that normalized text. A
        # hit-delta over the run is robust to that rewrite. (hits stays 0 for
        # alerts classified by the deterministic shortcut, which never call the
        # LLM — correctly reported as "no recorded LLM used".)
        hits_before = recorded_llm.hits
        await orch.run(ctx)
        llm_recorded = recorded_llm.hits > hits_before

        rec = capture.records.get(trace_id, {})
        updates = rec.get("updates", {})
        bucket = updates.get("bucket")
        result = ReplayResult(
            event_id=event_id,
            channel=channel,
            message=msg,
            bucket=bucket,
            action_taken=_infer_action(bucket, updates),
            side=updates.get("side"),
            ticker=updates.get("ticker"),
            final_status=rec.get("status", "unknown"),
            final_reason=terminal["reason"] or rec.get("reason"),
            would_be_orders=_summarize_orders(gateway.placed_orders),
            llm_recorded=llm_recorded,
        )
        return result
    finally:
        await conn.close()


async def replay_all(events, policy, recorded_llm, **kw) -> list[ReplayResult]:
    results = []
    for row in events:
        results.append(await replay_one(row, policy, recorded_llm, **kw))
    return results
