import pytest
from skills.execution.sizing_resolver import SizingResolver
from agent.policy import (SizingPolicy, SizingBuckets, SizingTier, ExecutionPolicy)
from agent.context import Context


def _policy_with_sizing() -> ExecutionPolicy:
    return ExecutionPolicy(
        sizing=SizingPolicy(
            default=SizingBuckets(
                high=SizingTier(shares=0.10, options=0.05),
                low=SizingTier(shares=0.05, options=0.05),
            ),
            per_channel={
                "stock-talk-portfolio": SizingBuckets(
                    high=SizingTier(shares=0.20, options=0.05),
                    low=SizingTier(shares=0.15, options=0.05),
                ),
                "mystic": SizingBuckets(
                    high=SizingTier(shares=0.15, options=0.05),
                    low=SizingTier(shares=0.10, options=0.05),
                ),
            },
        ),
    )


@pytest.mark.asyncio
async def test_per_channel_high():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"channel": "stock-talk-portfolio", "bucket": "HIGH"})
    await skill.run(ctx)
    assert ctx.get("shares_pct") == 0.20
    assert ctx.get("options_pct") == 0.05


@pytest.mark.asyncio
async def test_per_channel_low():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"channel": "mystic", "bucket": "LOW"})
    await skill.run(ctx)
    assert ctx.get("shares_pct") == 0.10
    assert ctx.get("options_pct") == 0.05


@pytest.mark.asyncio
async def test_default_for_unknown_channel():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"channel": "urkel", "bucket": "HIGH"})
    await skill.run(ctx)
    assert ctx.get("shares_pct") == 0.10
    assert ctx.get("options_pct") == 0.05


@pytest.mark.asyncio
async def test_skip_bucket_terminates():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"channel": "mystic", "bucket": "SKIP"})
    result = await skill.run(ctx)
    assert result.status == "skip"
