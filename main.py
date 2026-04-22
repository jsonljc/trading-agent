#!/usr/bin/env python3
"""
Trading agent Phase 1 entry point.
Reads trigger events from the Swift bridge Unix socket, runs the Phase 1 pipeline.

Usage:
    python main.py [--socket /tmp/trading_bridge.sock] [--db data/trading_agent.db] [--policy config/policy.yaml]
"""
import asyncio
import logging
import argparse
import uuid

from agent.policy import load_policy
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.registry import build_phase1_chain
from infra.storage.db import get_connection
from infra.storage.trace_store import TraceStore
from infra.storage.idempotency_store import IdempotencyStore
from infra.storage.signal_store import SignalStore
from infra.storage.execution_store import ExecutionStore
from infra.telegram.client import TelegramClient
from infra.bridge_client.socket_reader import SocketReader, TriggerEvent
from infra.bridge_client.notification_poller import NotificationBannerPoller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


async def run(socket_path: str, db_path: str, policy_path: str) -> None:
    policy = load_policy(policy_path)
    conn = await get_connection(db_path)

    signal_store = SignalStore(conn)
    trace_store = TraceStore(conn)
    idempotency_store = IdempotencyStore(conn)
    execution_store = ExecutionStore(conn)
    telegram = TelegramClient(
        bot_token=policy.telegram.bot_token,
        chat_id=policy.telegram.chat_id,
    )

    from infra.ib.gateway import IBGateway
    gateway = IBGateway(policy)
    await gateway.connect()

    from agent.registry import build_phase1_chain, build_phase2b_execution_chain
    from skills.execution.execution_audit_writer import ExecutionAuditWriter
    from skills.execution.execution_reconciler import ExecutionReconciler

    phase1_chain = build_phase1_chain(policy, idempotency_store, telegram)
    phase2b_chain = build_phase2b_execution_chain(policy, execution_store, gateway)
    full_chain = phase1_chain + phase2b_chain

    audit_writer = ExecutionAuditWriter(conn)
    digest_skill = phase1_chain[-1]

    async def on_fail(ctx: Context, reason: str) -> None:
        await audit_writer.write(ctx, "failed")
        await digest_skill.send_error_digest(ctx, reason)

    async def on_skip(ctx: Context, reason: str) -> None:
        await audit_writer.write(ctx, "skipped")

    orch = Orchestrator(full_chain, trace_store, on_skip=on_skip, on_fail=on_fail)

    async def handle_event(event: TriggerEvent) -> None:
        trace_id = str(uuid.uuid4())[:12]
        logger.info("Received event %s from #%s by %s", event.event_id, event.channel, event.author)

        await signal_store.insert({
            "id": event.event_id,
            "source": event.source,
            "channel": event.channel,
            "author": event.author,
            "trigger_preview": event.trigger_preview,
            "full_message_text": event.trigger_preview,
            "capture_mode": "bridge",
            "message_fingerprint": "",
            "received_at": event.received_at,
        })

        ctx = Context(trace_id=trace_id, event_id=event.event_id)
        ctx.update({
            "trigger_preview": event.trigger_preview,
            "full_message_text": event.trigger_preview,
            "channel": event.channel,
            "author": event.author,
            "received_at": event.received_at,
        })

        await orch.run(ctx)
        await audit_writer.write(ctx, "success")

    reconciler = ExecutionReconciler(
        gateway, execution_store,
        interval_seconds=policy.execution.reconciler_interval_seconds,
    )

    reader = SocketReader(socket_path)
    logger.info("Trading agent Phase 2b ready. Listening on %s", socket_path)
    try:
        NotificationBannerPoller().start()
        reconciler.start()
        await reader.start(handle_event)
    finally:
        await gateway.disconnect()
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/tmp/trading_bridge.sock")
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--policy", default="config/policy.yaml")
    args = parser.parse_args()
    asyncio.run(run(args.socket, args.db, args.policy))
