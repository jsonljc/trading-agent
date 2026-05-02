import pytest
from pathlib import Path
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.traders.profile import load_all_profiles
from agent.traders.registry import TraderRegistry
from skills.signal.trader_router import TraderRouter
from skills.signal.trader_classifier import TraderClassifier
from skills.signal.classification_logger import ClassificationLogger
from skills.signal.entry_skip_gate import EntrySkipGate
from infra.storage.classification_log_store import ClassificationLogStore
from infra.storage.trace_store import TraceStore


REPO_ROOT = Path(__file__).resolve().parents[2]


class StubLLM:
    def __init__(self, response: dict): self._r = response
    async def classify(self, **kw): return self._r


@pytest.mark.asyncio
async def test_phase1_high_bucket_signal(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)

    llm = StubLLM({"is_entry": True, "ticker": "AOSL", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "long idea"})
    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
        EntrySkipGate(),
    ]
    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "UndefinedMystic",
        "channel": "alerts",
        "full_message_text": "Alpha + Omega Semiconductor long idea — deep thesis @Alerts - Mystic",
    })
    await orch.run(ctx)

    rows = await log_store.recent_for_trader("mystic")
    assert len(rows) == 1
    assert rows[0]["bucket"] == "HIGH"
    assert rows[0]["size_pct"] == 0.10


@pytest.mark.asyncio
async def test_phase1_low_bucket_signal(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)

    llm = StubLLM({"is_entry": True, "ticker": "AUDC", "side": "long",
                   "bucket": "LOW", "confidence": 0.85, "reason": "small add"})
    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
        EntrySkipGate(),
    ]
    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine",
        "channel": "alerts",
        "full_message_text": "thinking about loading AUDC here @Wall - Alerts",
    })
    await orch.run(ctx)

    rows = await log_store.recent_for_trader("wallstengine")
    assert len(rows) == 1
    assert rows[0]["bucket"] == "LOW"
    assert rows[0]["size_pct"] == 0.05


@pytest.mark.asyncio
async def test_phase1_skip_signal(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)

    llm = StubLLM({"is_entry": False, "ticker": None, "side": "none",
                   "bucket": "SKIP", "confidence": 0.95, "reason": "macro"})
    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
        EntrySkipGate(),
    ]
    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly",
        "channel": "alerts",
        "full_message_text": "Markets have two phases @Stock Talk Weekly - Alerts",
    })
    await orch.run(ctx)

    rows = await log_store.recent_for_trader("stocktalkweekly")
    assert len(rows) == 1
    assert rows[0]["bucket"] == "SKIP"
