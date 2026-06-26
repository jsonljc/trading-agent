import json
import pytest
from agent.context import Context
from agent.traders.profile import TraderProfile, ConvictionExample
from agent.traders.registry import TraderRegistry
from skills.signal.trader_classifier import TraderClassifier


def make_profile(handle="wse", auto=True, size_in_msg=True, size_floor=None) -> TraderProfile:
    return TraderProfile(
        handle=handle, display_name="Wall St Engine",
        discord_author_pattern="Wall St Engine",
        alert_mention="@Wall - Alerts", require_alert_mention=True,
        bot_authors_to_skip=(), auto_execute=auto,
        size_in_message=size_in_msg, prefer_message_size=True,
        classifier_model="claude-haiku-4-5",
        availability_phrases=(),
        conviction_examples=(
            ConvictionExample(msg="Added 2% pos AUDC", bucket="LOW", why="2% small"),
            ConvictionExample(msg="upsizing core ENS aggressively", bucket="HIGH", why="upsize core"),
            ConvictionExample(msg="watching TEST closely", bucket="SKIP", why="no entry"),
        ),
        size_floor=size_floor,
    )


class FakeLLM:
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    async def classify(self, *, system: list, model: str, messages: list) -> dict:
        self.calls.append({"system": system, "model": model, "messages": messages})
        return self._response


@pytest.mark.asyncio
async def test_shortcut_path_uses_stated_size_no_llm_call():
    profile = make_profile()
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "AUDC", "side": "long",
                   "bucket": "LOW", "confidence": 0.5, "reason": "should not be used"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine",
        "trader_handle": "wse",
        "full_message_text": "Added 2% pos AUDC on back of earnings",
    })

    result = await classifier.run(ctx)

    assert result.status == "success"
    assert ctx.get("size_pct") in (None, 0.0)  # classifier no longer sets size_pct
    assert ctx.get("size_source") == "shortcut_stated"
    assert ctx.get("ticker") == "AUDC"
    assert ctx.get("bucket") == "LOW"
    assert llm.calls == [], "shortcut path must not call LLM synchronously"


@pytest.mark.asyncio
async def test_llm_path_high_confidence_high_bucket_fires_at_10pct():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "AOSL", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "long idea thesis"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "Alpha + Omega Semiconductor long idea — deep thesis...",
    })

    result = await classifier.run(ctx)

    assert result.status == "success"
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_pct") is None  # classifier no longer sets size_pct
    assert ctx.get("size_source") == "bucket_high"


@pytest.mark.asyncio
async def test_llm_path_mid_confidence_downgrades_to_low_5pct():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "X", "side": "long",
                   "bucket": "HIGH", "confidence": 0.65, "reason": "ambiguous"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "thinking about loading X here, looks interesting",
    })

    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "LOW"
    assert ctx.get("size_pct") is None  # classifier no longer sets size_pct
    assert ctx.get("size_source") == "downgrade"


@pytest.mark.asyncio
async def test_llm_path_low_confidence_drops():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "Z", "side": "long",
                   "bucket": "LOW", "confidence": 0.3, "reason": "very ambiguous"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "kind of interesting maybe",
    })

    result = await classifier.run(ctx)
    assert result.status == "success"
    assert "low_confidence" in (result.reason or "")
    assert ctx.get("size_pct") == 0.0
    # Regression: bucket MUST be "SKIP" so EntrySkipGate halts the pipeline.
    # If bucket leaks through as LLM's HIGH/LOW value, low-confidence signals
    # would execute at full per-channel sizing.
    assert ctx.get("bucket") == "SKIP"
    assert ctx.get("size_source") == "drop_low_conf"


@pytest.mark.asyncio
async def test_llm_skip_response_marks_bucket_skip():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": False, "ticker": None, "side": "none",
                   "bucket": "SKIP", "confidence": 0.9, "reason": "commentary"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "great results for $OSS — Revenue +70% Y/Y",
    })

    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("bucket") == "SKIP"


@pytest.mark.asyncio
async def test_stated_size_capped_at_10pct():
    profile = make_profile()
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "XX", "side": "long",
                   "bucket": "LOW", "confidence": 0.9, "reason": "x"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "Added 20% pos in XX",
    })

    result = await classifier.run(ctx)
    assert ctx.get("size_pct") in (None, 0.0)  # classifier no longer sets size_pct
    assert ctx.get("size_source") == "shortcut_stated"
    assert ctx.get("ticker") == "XX"


@pytest.mark.asyncio
async def test_shortcut_threshold_at_7_5_pct_buckets_high():
    profile = make_profile()
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "X", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "x"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "Added 7.5% pos AAPL",
    })

    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("size_pct") in (None, 0.0)  # classifier no longer sets size_pct
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_source") == "shortcut_stated"


class ExplodingLLM:
    async def classify(self, **kw):
        raise TimeoutError("timeout")


@pytest.mark.asyncio
async def test_llm_error_returns_success_with_skip_for_audit_logging():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    classifier = TraderClassifier(registry, ExplodingLLM())
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "ambiguous content here",
    })

    result = await classifier.run(ctx)
    assert result.status == "success"  # not "fail" — must reach logger
    assert ctx.get("bucket") == "SKIP"
    assert ctx.get("size_pct") == 0.0
    assert ctx.get("size_source") == "llm_error"
    assert "TimeoutError" in (ctx.get("classifier_reason") or "")


@pytest.mark.asyncio
async def test_llm_returns_ticker_not_in_message_skips():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "FAKE", "side": "long",
                   "bucket": "LOW", "confidence": 0.9, "reason": "hallucinated"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "thinking about loading AAPL here",  # only AAPL, no FAKE
    })

    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("bucket") == "SKIP"
    assert ctx.get("size_source") == "ticker_not_in_msg"


@pytest.mark.asyncio
async def test_stw_size_floor_shortcut_forces_high():
    profile = make_profile(handle="stocktalkweekly", size_floor="HIGH")
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "SEI", "side": "long",
                   "bucket": "LOW", "confidence": 0.5, "reason": "unused"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly", "trader_handle": "stocktalkweekly",
        "full_message_text": "OPENING $SEI with a small 1% pos",
    })
    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("bucket") == "HIGH"          # 1% would be LOW without the floor
    assert ctx.get("size_source") == "shortcut_stated"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_stw_size_floor_llm_path_forces_high():
    profile = make_profile(handle="stocktalkweekly", size_in_msg=False, size_floor="HIGH")
    registry = TraderRegistry([profile])
    # 0.65 confidence would normally downgrade HIGH->LOW; no entry verb so the
    # shortcut does not fire and we exercise the LLM path.
    llm = FakeLLM({"is_entry": True, "ticker": "ADEA", "side": "long",
                   "bucket": "HIGH", "confidence": 0.65, "reason": "thesis"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly", "trader_handle": "stocktalkweekly",
        "full_message_text": "ADEIA $ADEA multi-pillar thesis, a 2% position",
    })
    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_source") == "size_floor"


@pytest.mark.asyncio
async def test_stw_size_floor_does_not_rescue_low_confidence_skip():
    profile = make_profile(handle="stocktalkweekly", size_in_msg=False, size_floor="HIGH")
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "SEI", "side": "long",
                   "bucket": "LOW", "confidence": 0.3, "reason": "ambiguous"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly", "trader_handle": "stocktalkweekly",
        "full_message_text": "maybe SEI here",
    })
    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "SKIP"          # floor must not override the SKIP guard
    assert ctx.get("size_source") == "drop_low_conf"


@pytest.mark.asyncio
async def test_stw_size_floor_lifts_after_wse_small_size_override():
    # High-confidence HIGH + small stated size: the wse_small_size_override
    # knocks HIGH->LOW, then size_floor must lift it back to HIGH. Pins the
    # ordering of the two overrides in the LLM path.
    profile = make_profile(handle="stocktalkweekly", size_in_msg=False, size_floor="HIGH")
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "ADEA", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "moat thesis"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly", "trader_handle": "stocktalkweekly",
        "full_message_text": "ADEA 2% weighting -- strong moat thesis",  # no entry verb
    })
    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_source") == "size_floor"


@pytest.mark.asyncio
async def test_wse_small_size_override_unaffected_without_floor():
    profile = make_profile(handle="wse", size_in_msg=False, size_floor=None)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "AUDC", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "thesis"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "AUDC 2% weighting compelling setup",  # no entry verb
    })
    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "LOW"
    assert ctx.get("size_source") == "wse_small_size_override"


@pytest.mark.asyncio
async def test_shortcut_does_not_fire_on_profit_taking_exit():
    """A profit-taking message that states a remaining size must NOT take the
    deterministic BUY shortcut (which hardcodes side=long, confidence=1.0).
    'took profits' is an exit verb, so the message must fall through to the LLM
    — which classifies it SKIP — rather than auto-firing a BUY."""
    profile = make_profile()  # prefer_message_size=True
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": False, "ticker": None, "side": "none",
                   "bucket": "SKIP", "confidence": 0.95, "reason": "profit taking, not an entry"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "took profits on $NVDA, still holding a 5% position",
    })

    result = await classifier.run(ctx)

    assert result.status == "success"
    assert len(llm.calls) == 1, "exit phrasing must reach the LLM, not the shortcut"
    assert ctx.get("size_source") != "shortcut_stated"
    assert ctx.get("bucket") == "SKIP"
