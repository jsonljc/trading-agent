import pytest
from agent.context import Context
from skills.signal.entry_skip_gate import EntrySkipGate


@pytest.mark.asyncio
async def test_skips_when_bucket_is_none():
    g = EntrySkipGate()
    ctx = Context(trace_id="t1", event_id="e1")
    result = await g.run(ctx)
    assert result.status == "skip"
    assert "bucket=None" in result.reason


@pytest.mark.asyncio
async def test_skips_when_bucket_is_skip():
    g = EntrySkipGate()
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"bucket": "SKIP"})
    result = await g.run(ctx)
    assert result.status == "skip"
    assert "bucket=SKIP" in result.reason


@pytest.mark.asyncio
async def test_passes_for_actionable_bucket_without_size_pct():
    """After classifier rework, size_pct is no longer set for actionable signals."""
    g = EntrySkipGate()
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"bucket": "HIGH"})  # no size_pct in ctx
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_passes_for_low_bucket_without_size_pct():
    g = EntrySkipGate()
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"bucket": "LOW"})
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_passes_for_high_bucket_with_size_pct():
    """Gate ignores size_pct entirely; bucket alone determines pass/skip."""
    g = EntrySkipGate()
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"bucket": "HIGH", "size_pct": 0.05})
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_skips_even_when_size_pct_present_but_bucket_skip():
    """size_pct in ctx does not override a SKIP bucket."""
    g = EntrySkipGate()
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"bucket": "SKIP", "size_pct": 0.05})
    result = await g.run(ctx)
    assert result.status == "skip"
