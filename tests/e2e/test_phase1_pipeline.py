import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.policy import PolicyModel
from infra.storage.idempotency_store import IdempotencyStore
from infra.storage.trace_store import TraceStore
from skills.signal.message_normalizer import MessageNormalizer
from skills.signal.desktop_reader import DesktopReader
from skills.signal.trade_intent_detector import TradeIntentDetector
from skills.risk.idempotency_check import IdempotencyCheck
from skills.signal.ticker_resolver import TickerResolver
from skills.signal.conviction_classifier import ConvictionClassifier
from skills.posttrade.telegram_digest import TelegramDigest
import yaml


def make_policy():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    return PolicyModel.model_validate(yaml.safe_load(config_path.read_text()))


def claude_response(payload: dict):
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps(payload))]
    return m


def make_ctx(preview: str = "Long $AVEX today, starting a position here with high conviction") -> Context:
    ctx = Context(trace_id="trace-e2e-1", event_id="evt-1")
    ctx.update({
        "trigger_preview": preview,
        "full_message_text": preview,
        "channel": "mystic",
        "author": "Mystic",
        "received_at": "2026-04-18T10:00:00Z",
    })
    return ctx


def build_chain(policy, idempotency_store, telegram):
    digest = TelegramDigest(telegram, mode="signal_only")
    chain = [
        MessageNormalizer(policy),
        DesktopReader(policy),
        TradeIntentDetector(policy),
        IdempotencyCheck(policy, idempotency_store),
        TickerResolver(policy),
        ConvictionClassifier(policy),
        digest,
    ]
    return chain, digest


# chain[2] = TradeIntentDetector, chain[4] = TickerResolver, chain[5] = ConvictionClassifier
# MessageNormalizer (0) and DesktopReader (1) have no LLM calls
# IdempotencyCheck (3) uses DB, no LLM

async def test_happy_path_sends_digest(db, telegram):
    policy = make_policy()
    idempotency_store = IdempotencyStore(db)
    trace_store = TraceStore(db)
    chain, digest = build_chain(policy, idempotency_store, telegram)

    with patch.object(chain[2]._client.messages, "create",
                      AsyncMock(return_value=claude_response({"intent": "LONG_SIGNAL", "confidence": "high", "reason": "explicit long"}))):
        with patch.object(chain[4]._client.messages, "create",
                          AsyncMock(return_value=claude_response({"ticker": "AVEX", "ambiguous": False, "asset_type_hint": "equity"}))):
            with patch.object(chain[5]._client.messages, "create",
                              AsyncMock(return_value=claude_response({"conviction_bucket": "high", "reason": "high conv"}))):
                orch = Orchestrator(chain, trace_store)
                ctx = make_ctx()
                await orch.run(ctx)

    assert len(telegram.sent) == 1
    assert "AVEX" in telegram.sent[0]
    assert "LONG_SIGNAL" in telegram.sent[0]


async def test_no_action_skips_no_digest(db, telegram):
    policy = make_policy()
    idempotency_store = IdempotencyStore(db)
    trace_store = TraceStore(db)
    chain, digest = build_chain(policy, idempotency_store, telegram)

    with patch.object(chain[2]._client.messages, "create",
                      AsyncMock(return_value=claude_response({"intent": "NO_ACTION", "confidence": "high", "reason": "just watching"}))):
        orch = Orchestrator(chain, trace_store)
        ctx = make_ctx("Watching $AVEX closely, looks interesting but not acting yet")
        await orch.run(ctx)

    assert len(telegram.sent) == 0


async def test_duplicate_signal_skips(db, telegram):
    policy = make_policy()
    idempotency_store = IdempotencyStore(db)
    trace_store = TraceStore(db)
    chain, digest = build_chain(policy, idempotency_store, telegram)

    with patch.object(chain[2]._client.messages, "create",
                      AsyncMock(return_value=claude_response({"intent": "LONG_SIGNAL", "confidence": "high", "reason": "long"}))):
        with patch.object(chain[4]._client.messages, "create",
                          AsyncMock(return_value=claude_response({"ticker": "AVEX", "ambiguous": False, "asset_type_hint": "equity"}))):
            with patch.object(chain[5]._client.messages, "create",
                              AsyncMock(return_value=claude_response({"conviction_bucket": "low", "reason": "small"}))):
                orch = Orchestrator(chain, trace_store)
                # First run
                await orch.run(make_ctx())
                sent_after_first = len(telegram.sent)
                # Second run with same text — same fingerprint, should be deduped
                await orch.run(make_ctx())

    assert sent_after_first == 1
    assert len(telegram.sent) == 1


async def test_ambiguous_ticker_fires_error_digest(db, telegram):
    policy = make_policy()
    idempotency_store = IdempotencyStore(db)
    trace_store = TraceStore(db)
    chain, digest_skill = build_chain(policy, idempotency_store, telegram)

    async def on_fail(ctx, reason):
        await digest_skill.send_error_digest(ctx, reason)

    with patch.object(chain[2]._client.messages, "create",
                      AsyncMock(return_value=claude_response({"intent": "LONG_SIGNAL", "confidence": "high", "reason": "long"}))):
        with patch.object(chain[4]._client.messages, "create",
                          AsyncMock(return_value=claude_response({"ticker": None, "ambiguous": True, "asset_type_hint": "equity"}))):
            orch = Orchestrator(chain, trace_store, on_fail=on_fail)
            ctx = make_ctx("Long the AI names, initiating a position across the basket")
            await orch.run(ctx)

    assert len(telegram.sent) == 1
    assert "ERROR" in telegram.sent[0]
    assert "ambiguous" in telegram.sent[0].lower()
