import pytest
from pathlib import Path
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.traders.profile import load_all_profiles
from agent.traders.registry import TraderRegistry
from skills.signal.trader_router import TraderRouter
from skills.signal.trader_classifier import TraderClassifier
from infra.storage.classification_log_store import ClassificationLogStore
from infra.storage.trace_store import TraceStore


REPO_ROOT = Path(__file__).resolve().parents[2]


class StubLLM:
    def __init__(self, response): self._r = response
    async def classify(self, **kw): return self._r


class CapturingTelegram:
    def __init__(self): self.sent = []
    async def send_message(self, text): self.sent.append(text)


@pytest.mark.asyncio
async def test_wse_shortcut_path_logs_classification_no_llm_call(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)

    llm = StubLLM({"is_entry": False, "ticker": None, "side": "none",
                   "bucket": "SKIP", "confidence": 0.99, "reason": "noop"})

    from skills.signal.classification_logger import ClassificationLogger
    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
    ]

    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t1", event_id="e1", data={
        "author": "Wall St Engine",
        "channel": "alerts",
        "full_message_text": "Added 2% pos AUDC @Wall - Alerts",
    })
    await orch.run(ctx)

    rows = await log_store.recent_for_trader("wallstengine")
    assert len(rows) == 1
    assert rows[0]["size_source"] == "shortcut_stated"
    assert rows[0]["llm_response_json"] is None


@pytest.mark.asyncio
async def test_skip_classifications_are_logged(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)

    llm = StubLLM({"is_entry": False, "ticker": None, "side": "none",
                   "bucket": "SKIP", "confidence": 0.92, "reason": "macro commentary"})

    from skills.signal.classification_logger import ClassificationLogger
    from skills.signal.entry_skip_gate import EntrySkipGate

    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
        EntrySkipGate(),
    ]
    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t3", event_id="e3", data={
        "author": "Stock Talk Weekly",
        "channel": "alerts",
        "full_message_text": "Yet another intraday fade in the market with $SPY $QQQ flushing red @Stock Talk Weekly - Alerts",
    })
    await orch.run(ctx)

    rows = await log_store.recent_for_trader("stocktalkweekly")
    assert len(rows) == 1
    assert rows[0]["bucket"] == "SKIP"
    assert rows[0]["action_taken"] == "skipped"


@pytest.mark.asyncio
async def test_mystic_bootstrap_mode_posts_to_telegram_and_skips(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)
    telegram_client = CapturingTelegram()

    llm = StubLLM({"is_entry": True, "ticker": "INDI", "side": "long",
                   "bucket": "LOW", "confidence": 0.85,
                   "reason": "small swing trade self-label"})

    from skills.signal.classification_logger import ClassificationLogger
    from skills.signal.bootstrap_review_gate import BootstrapReviewGate
    from skills.posttrade.telegram_digest import TelegramDigest

    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
        BootstrapReviewGate(TelegramDigest(telegram_client)),
    ]
    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t2", event_id="e2", data={
        "author": "UndefinedMystic",
        "channel": "alerts",
        "full_message_text": "i opened a small swing trade position in INDI @Alerts - Mystic",
    })
    await orch.run(ctx)

    assert any("BOOTSTRAP REVIEW" in m for m in telegram_client.sent)
    rows = await log_store.recent_for_trader("mystic")
    assert rows[0]["action_taken"] == "bootstrap_review"
