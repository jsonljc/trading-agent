"""Tests for the live-LLM response recorder used to build a REAL accuracy cache.

The committed responses_sample.jsonl is an *ideal oracle* (response == ground
truth → tautological 100%). To measure true accuracy you must capture what the
model ACTUALLY returns. RecordingLLM wraps the live client, memoizes repeat
messages (the eval classifies each message once per trader-subset AND pooled,
so the same message hits classify() several times — we must not pay for / record
it more than once), and flushes a deduped msg->response cache that RecordedLLM
can replay offline.
"""
import json
from pathlib import Path

import pytest
import importlib.util

_BIN = Path(__file__).resolve().parents[2] / "bin" / "eval_classifiers.py"
_spec = importlib.util.spec_from_file_location("eval_classifiers", _BIN)
eval_classifiers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_classifiers)
RecordingLLM = eval_classifiers.RecordingLLM
RecordedLLM = eval_classifiers.RecordedLLM
load_responses = eval_classifiers.load_responses


class CountingLLM:
    def __init__(self):
        self.calls = 0

    async def classify(self, *, system, model, messages):
        self.calls += 1
        return {"is_entry": True, "ticker": "X", "side": "long",
                "bucket": "HIGH", "confidence": 0.9, "_call": self.calls}


@pytest.mark.asyncio
async def test_recording_llm_memoizes_repeat_messages(tmp_path):
    inner = CountingLLM()
    out = tmp_path / "rec.jsonl"
    rec = RecordingLLM(inner, out)
    msg = [{"role": "user", "content": "Added a 5% position in $AAPL"}]
    r1 = await rec.classify(system=[], model="m", messages=msg)
    r2 = await rec.classify(system=[], model="m", messages=msg)
    assert inner.calls == 1            # repeat message must NOT re-hit the live LLM
    assert r1 == r2
    assert rec.flush() == 1
    lines = [l for l in out.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec0 = json.loads(lines[0])
    assert rec0["msg"] == "Added a 5% position in $AAPL"
    assert rec0["response"]["bucket"] == "HIGH"


@pytest.mark.asyncio
async def test_recording_llm_cache_replays_via_recorded_llm(tmp_path):
    """The flushed file must be consumable by RecordedLLM (round-trip)."""
    inner = CountingLLM()
    out = tmp_path / "rec.jsonl"
    rec = RecordingLLM(inner, out)
    await rec.classify(system=[], model="m",
                       messages=[{"role": "user", "content": "msg one"}])
    await rec.classify(system=[], model="m",
                       messages=[{"role": "user", "content": "msg two"}])
    assert rec.flush() == 2
    replay = RecordedLLM(load_responses(out))
    got = await replay.classify(system=[], model="m",
                                messages=[{"role": "user", "content": "msg two"}])
    assert got["bucket"] == "HIGH"


def test_require_api_key_raises_when_missing(monkeypatch):
    # Guards the silent-garbage trap: without a key the live path errors every
    # call and the classifier forces SKIP, yielding a fake low "accuracy" rather
    # than a clear failure. The live/record path must refuse to run instead.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        eval_classifiers._require_api_key()


def test_require_api_key_passes_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    eval_classifiers._require_api_key()  # must not raise
