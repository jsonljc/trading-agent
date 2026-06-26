#!/usr/bin/env python3
from dotenv import load_dotenv
load_dotenv()
"""
Trading agent Phase 1 entry point.
Reads trigger events from the Swift bridge Unix socket, runs the Phase 1 pipeline.

Usage:
    python main.py [--socket /tmp/trading_bridge.sock] [--db data/trading_agent.db] [--policy config/policy.yaml]
"""
import asyncio
import logging
import logging.handlers
import argparse
import os
import uuid

from pathlib import Path
import anthropic
from agent.policy import load_policy
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.registry import build_phase1_chain
from agent.traders.profile import load_all_profiles
from agent.traders.registry import TraderRegistry
from infra.storage.db import get_connection
from infra.storage.trace_store import TraceStore
from infra.storage.idempotency_store import IdempotencyStore
from infra.storage.signal_store import SignalStore
from infra.storage.execution_store import ExecutionStore
from infra.storage.classification_log_store import ClassificationLogStore
from infra.storage.trim_ladder_store import TrimLadderStore
from infra.llm.classifier_client import AnthropicClassifierClient
from infra.telegram.client import TelegramClient
from infra.bridge_client.socket_reader import SocketReader, TriggerEvent
from skills.signal.message_normalizer import compute_fingerprint
from agent.exit_ladder import ExitLadder

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
os.makedirs("logs", exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    "logs/agent.log", maxBytes=10_000_000, backupCount=5
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("main")


async def run(socket_path: str, db_path: str, policy_path: str) -> None:
    policy = load_policy(policy_path)
    conn = await get_connection(db_path)

    # Startup DB health: integrity check + an off-machine snapshot. A torn WAL
    # after an unclean shutdown otherwise stays silent until it corrupts a write.
    from infra.storage.db import check_integrity, backup_database
    try:
        integrity = await check_integrity(conn)
        if integrity != "ok":
            logger.error("DB integrity_check FAILED: %s", integrity)
        else:
            logger.info("DB integrity_check ok")
            try:
                from datetime import datetime, timezone
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                dest = os.path.join("data", "backups", f"agent-{stamp}.db")
                await backup_database(conn, dest)
                logger.info("DB snapshot written to %s", dest)
            except Exception:
                logger.exception("DB backup failed (non-fatal)")
    except Exception:
        logger.exception("DB integrity check failed to run (non-fatal)")

    signal_store = SignalStore(conn)
    trace_store = TraceStore(conn)
    idempotency_store = IdempotencyStore(conn)
    execution_store = ExecutionStore(conn)
    classification_log_store = ClassificationLogStore(conn)
    trader_profiles = load_all_profiles(Path("config/traders"))
    trader_registry = TraderRegistry(trader_profiles)
    llm_classifier = AnthropicClassifierClient(anthropic.AsyncAnthropic())
    from infra.storage.trade_intent_store import TradeIntentStore
    trade_intent_store = TradeIntentStore(conn)
    telegram = TelegramClient(
        bot_token=policy.telegram.bot_token,
        chat_id=policy.telegram.chat_id,
    )

    from infra.ib.gateway import IBGateway

    async def _on_ib_disconnect() -> None:
        try:
            await telegram.send_message("⚠️ IB Gateway disconnected — reconnect loop started")
        except Exception:
            logger.exception("failed to send IB disconnect alert")

    async def _on_ib_reconnect() -> None:
        try:
            await telegram.send_message("✅ IB Gateway reconnected")
        except Exception:
            logger.exception("failed to send IB reconnect alert")

    async def _on_ib_reconnect_failing(elapsed_minutes: int) -> None:
        try:
            await telegram.send_message(
                f"🚨 IB Gateway still offline after {elapsed_minutes} minutes — "
                f"signals are being silently dropped. Check the gateway process."
            )
        except Exception:
            logger.exception("failed to send IB reconnect-failing alert")

    gateway = IBGateway(
        policy,
        on_disconnect=_on_ib_disconnect,
        on_reconnect=_on_ib_reconnect,
        on_reconnect_failing=_on_ib_reconnect_failing,
    )
    await gateway.connect()

    from agent.registry import build_phase1_chain, build_phase2b_execution_chain
    from skills.execution.execution_audit_writer import ExecutionAuditWriter
    from skills.execution.execution_reconciler import ExecutionReconciler

    trim_store = TrimLadderStore(conn)
    from infra.storage.position_exit_store import PositionExitStore
    exits_store = PositionExitStore(conn)

    phase1_chain = build_phase1_chain(
        policy, idempotency_store, telegram,
        gateway=gateway,
        trader_registry=trader_registry,
        classification_log_store=classification_log_store,
        llm_classifier=llm_classifier,
        trade_intent_store=trade_intent_store,
        exits_store=exits_store,
    )
    phase2b_chain = build_phase2b_execution_chain(
        policy, execution_store, gateway, trade_intent_store, trim_store=trim_store
    )
    full_chain = phase1_chain + phase2b_chain

    audit_writer = ExecutionAuditWriter(conn)
    digest_skill = phase1_chain[-1]

    async def on_fail(ctx: Context, reason: str) -> None:
        from skills.posttrade.telegram_digest import TelegramDigest
        await audit_writer.write(ctx, "failed")
        # A hard broker rejection gets a distinct alert (it lands in the DLQ);
        # everything else is the generic error digest.
        if TelegramDigest.is_order_rejected(reason):
            await digest_skill.send_order_rejected_alert(ctx, reason)
        else:
            await digest_skill.send_error_digest(ctx, reason)

    async def on_skip(ctx: Context, reason: str) -> None:
        from skills.posttrade.telegram_digest import TelegramDigest
        # A followed sell executed a REAL order; do NOT audit it as 'skipped'
        # (that would corrupt P&L/fill-rate analytics) — record it distinctly
        # and send the sell digest. A partial-then-broker-down sell ALSO really
        # sold shares, so it is an executed sell (with a warning), not 'skipped'.
        if reason == "sell_followed":
            await audit_writer.write(ctx, "sell_followed")
            await digest_skill.send_sell_digest(ctx)
            return
        if reason.startswith("sell_partial_broker_unavailable"):
            await audit_writer.write(ctx, "sell_followed")
            await digest_skill.send_sell_digest(ctx)  # qty>0 -> sends
            await digest_skill.send_missed_signal_alert(
                ctx, f"sell partially executed, broker then down: {reason}")
            return
        await audit_writer.write(ctx, "skipped")
        # Informational alerts for sell outcomes that did NOT execute at all.
        if reason in ("no_open_position", "sell_outside_rth", "sell_zero_fill",
                      "sell_already_followed") or \
                reason.startswith("sell_broker_unavailable"):
            await digest_skill.send_missed_signal_alert(
                ctx, f"sell not executed: {reason}")
            return
        # Surface broker-unavailable skips on actionable signals — without this
        # the agent silently drops every fired classification while IB is down
        # (see ADEA on 2026-05-11: gateway dropped 19:45 ET, ADEA HIGH fires
        # at 20:06 and 20:47 dropped with reason 'circuit open').
        if TelegramDigest.is_broker_unavailable_skip(ctx, reason):
            await digest_skill.send_missed_signal_alert(ctx, reason)
        elif reason == "kill_switch_engaged" and ctx.get("bucket") in ("HIGH", "LOW"):
            # The operator engaged it deliberately, but still surface that an
            # actionable entry was dropped (consistent with broker-down alerts).
            await digest_skill.send_missed_signal_alert(
                ctx, "kill switch engaged — new entry halted")
        elif TelegramDigest.is_missed_entry_skip(ctx, reason):
            # DEGRADED/MISSED entry-skips the agent would otherwise drop SILENTLY:
            # classifier llm_error / drop_low_conf / ticker_not_in_msg (halts at
            # EntrySkipGate as "no_entry:bucket=SKIP"), unknown author on a tracked
            # channel (TraderRouter "no_trader_profile:"), or an actionable entry
            # fired off-session (RthEntryGuard "entry_outside_rth"). A *genuine*
            # commentary SKIP (size_source="skip") deliberately does NOT match, so
            # this stays quiet on the common, correct case.
            await digest_skill.send_missed_signal_alert(
                ctx, TelegramDigest.missed_entry_reason(ctx, reason))

    async def on_success(ctx: Context) -> None:
        await audit_writer.write(ctx, "success")
        partial = ctx.get("partial_execution_reason")
        shares_intent_id = ctx.get("shares_intent_id")
        if partial and shares_intent_id:
            try:
                await trade_intent_store.update_partial_execution_reason(
                    shares_intent_id, partial
                )
            except Exception:
                logger.exception("failed to persist partial_execution_reason")
        await digest_skill.send_fill_digest(ctx)

    orch = Orchestrator(full_chain, trace_store, on_skip=on_skip, on_fail=on_fail, on_success=on_success)

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
            "message_fingerprint": compute_fingerprint(
                event.channel, event.author, event.trigger_preview),
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

    async def _on_reconcile_discrepancy(summary: dict) -> None:
        lines = []
        for v in summary.get("vanished", []):
            tag = "likely FILLED while down" if v["in_position"] else "vanished"
            lines.append(f"• {v['ticker']} intent {v['intent_id']} — {tag}")
        for o in summary.get("orphans", []):
            lines.append(f"• orphan IB order {o['order_id']} ({o['order_ref']})")
        if not lines:
            return
        try:
            await telegram.send_message(
                "🔎 <b>RECONCILER</b> — broker/db mismatch, manual review:\n"
                + "\n".join(lines)
            )
        except Exception:
            logger.exception("failed to send reconciler discrepancy alert")

    reconciler = ExecutionReconciler(
        gateway, execution_store, trade_intent_store,
        interval_seconds=policy.execution.reconciler_interval_seconds,
        on_discrepancy=_on_reconcile_discrepancy,
    )

    exit_ladder = ExitLadder(
        gateway,
        trade_intent_store,
        trim_store,
        exits_store,
        poll_interval_seconds=policy.execution.exit_poll_interval_seconds,
        slippage_cap_pct=policy.execution.shares_slippage_cap_pct,
    )

    async def _on_bridge_parse_error(raw: str, err: str) -> None:
        try:
            await telegram.send_message(
                f"⚠️ <b>DROPPED SIGNAL</b> — a bridge event failed to parse and was "
                f"dead-lettered (the Chrome extension is the only capture path).\n"
                f"Error: {err}"
            )
        except Exception:
            logger.exception("failed to send bridge parse-error alert")

    from agent.heartbeat import Heartbeat
    heartbeat = Heartbeat(
        policy.execution.heartbeat_url,
        interval_seconds=policy.execution.heartbeat_interval_seconds,
    )

    reader = SocketReader(
        socket_path,
        deadletter_path="logs/bridge_deadletter.jsonl",
        on_parse_error=_on_bridge_parse_error,
    )
    logger.info("Trading agent Phase 2b ready. Listening on %s", socket_path)
    try:
        reconciler.start()
        exit_ladder.start()
        heartbeat.start()
        await reader.start(handle_event)
    finally:
        await heartbeat.stop()
        await exit_ladder.stop()
        await reconciler.stop()
        await digest_skill.drain()   # flush any in-flight (non-blocking) signal digests
        await gateway.disconnect()
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/tmp/trading_bridge.sock")
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--policy", default="config/policy.yaml")
    args = parser.parse_args()
    asyncio.run(run(args.socket, args.db, args.policy))
