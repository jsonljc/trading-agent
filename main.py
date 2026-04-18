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
from infra.telegram.client import TelegramClient
from infra.bridge_client.socket_reader import SocketReader, TriggerEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


async def run(socket_path: str, db_path: str, policy_path: str) -> None:
    policy = load_policy(policy_path)
    conn = await get_connection(db_path)

    signal_store = SignalStore(conn)
    trace_store = TraceStore(conn)
    idempotency_store = IdempotencyStore(conn)
    telegram = TelegramClient(
        bot_token=policy.telegram.bot_token,
        chat_id=policy.telegram.chat_id,
    )

    chain = build_phase1_chain(policy, idempotency_store, telegram)
    digest_skill = chain[-1]  # telegram_digest is always last

    async def on_fail(ctx: Context, reason: str) -> None:
        await digest_skill.send_error_digest(ctx, reason)

    async def on_skip(ctx: Context, reason: str) -> None:
        pass  # Phase 1: no digest on skip (NO_ACTION / WATCHLIST are noisy)

    orch = Orchestrator(chain, trace_store, on_skip=on_skip, on_fail=on_fail)

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

    reader = SocketReader(socket_path)
    logger.info("Trading agent Phase 1 ready. Listening on %s", socket_path)
    try:
        await reader.start(handle_event)
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/tmp/trading_bridge.sock")
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--policy", default="config/policy.yaml")
    args = parser.parse_args()
    asyncio.run(run(args.socket, args.db, args.policy))
