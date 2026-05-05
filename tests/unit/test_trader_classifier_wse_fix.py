import pytest
from unittest.mock import AsyncMock
from skills.signal.trader_classifier import TraderClassifier
from agent.traders.profile import TraderProfile
from agent.context import Context

WSE = TraderProfile(
    handle="wallstengine", display_name="WSE",
    discord_author_pattern="WSE", alert_mention="",
    require_alert_mention=False, bot_authors_to_skip=(),
    auto_execute=True, size_in_message=True, prefer_message_size=True,
    classifier_model="x", availability_phrases=(), conviction_examples=(),
)


class _Reg:
    def all(self): return [WSE]


def _llm(bucket="HIGH", confidence=0.85, ticker="CEG", side="long"):
    m = AsyncMock()
    m.classify.return_value = {
        "is_entry": True, "ticker": ticker, "side": side,
        "bucket": bucket, "confidence": confidence, "reason": "thesis",
    }
    return m


def _ctx(msg: str, handle: str = "wallstengine") -> Context:
    return Context(trace_id="t", event_id="e", data={
        "trader_handle": handle,
        "full_message_text": msg,
    })


@pytest.mark.asyncio
async def test_wse_small_size_overrides_llm_high():
    """3% pos with multi-ticker → LLM path; LLM says HIGH; we force LOW."""
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = _ctx("Added 3% pos in $CEG, paired with $VST exposure.")
    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("bucket") == "LOW"
    assert ctx.get("size_source") == "wse_small_size_override"


@pytest.mark.asyncio
async def test_high_stated_size_does_not_trigger_override():
    """10% stated → no override; LLM HIGH stays HIGH."""
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = _ctx("Added 10% pos in $CEG, $VST.")
    await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"


@pytest.mark.asyncio
async def test_no_stated_size_no_override():
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = _ctx("OPEN $CEG, $VST structured thesis.")
    await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"


@pytest.mark.asyncio
async def test_shortcut_sets_bucket_only_no_size_pct():
    """Shortcut sets bucket; size_pct is no longer set by the classifier."""
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = _ctx("Added 5% pos in $CEG.")
    await classifier.run(ctx)
    assert ctx.get("bucket") == "LOW"  # 5% < 7.5
    assert ctx.get("size_source") == "shortcut_stated"
    # size_pct should not be set by the classifier anymore
    assert ctx.get("size_pct") in (None, 0.0)


@pytest.mark.asyncio
async def test_shortcut_high_for_large_stated_size():
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = _ctx("Added 12% pos in $CEG.")
    await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_source") == "shortcut_stated"
