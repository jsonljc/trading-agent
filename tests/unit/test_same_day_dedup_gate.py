import pytest
from unittest.mock import AsyncMock

from agent.context import Context
from skills.signal.same_day_dedup_gate import SameDayDedupGate


def _ctx(*, bucket="HIGH", ticker="FIVN", side="long",
         trader="stocktalkweekly") -> Context:
    c = Context(trace_id="t1", event_id="e1")
    c.update({
        "bucket": bucket, "ticker": ticker, "side": side,
        "trader_handle": trader,
    })
    return c


@pytest.mark.asyncio
async def test_passes_when_no_prior_fire():
    store = AsyncMock()
    store.has_fired_recently = AsyncMock(return_value=False)
    gate = SameDayDedupGate(store, window_hours=24)
    result = await gate.run(_ctx())
    assert result.status == "success"


@pytest.mark.asyncio
async def test_skips_when_recent_fire_exists():
    store = AsyncMock()
    store.has_fired_recently = AsyncMock(return_value=True)
    gate = SameDayDedupGate(store, window_hours=24)
    ctx = _ctx()
    result = await gate.run(ctx)
    assert result.status == "skip"
    assert "same_day_dedup" in result.reason
    assert ctx.get("bucket") == "SKIP"
    assert ctx.get("size_source") == "dedup"


@pytest.mark.asyncio
async def test_ignores_non_actionable_buckets():
    store = AsyncMock()
    store.has_fired_recently = AsyncMock(return_value=True)
    gate = SameDayDedupGate(store, window_hours=24)
    # Even with a prior fire in the DB, SKIP/None buckets are not our concern
    result = await gate.run(_ctx(bucket="SKIP"))
    assert result.status == "success"
    store.has_fired_recently.assert_not_called()


@pytest.mark.asyncio
async def test_handles_missing_ticker():
    store = AsyncMock()
    store.has_fired_recently = AsyncMock(return_value=True)
    gate = SameDayDedupGate(store, window_hours=24)
    ctx = _ctx(ticker=None)
    result = await gate.run(ctx)
    # No ticker = nothing to dedup against; pass through
    assert result.status == "success"
    store.has_fired_recently.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_keyed_on_side():
    """A long fire today should not block a short fire on the same ticker."""
    store = AsyncMock()

    async def maybe_fired(*, trader_handle, ticker, side, hours,
                          exclude_event_id=None):
        return side == "long"  # only long has fired

    store.has_fired_recently = maybe_fired
    gate = SameDayDedupGate(store, window_hours=24)

    long_result = await gate.run(_ctx(side="long"))
    assert long_result.status == "skip"

    short_result = await gate.run(_ctx(side="short"))
    assert short_result.status == "success"
