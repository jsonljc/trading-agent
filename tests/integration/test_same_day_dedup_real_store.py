import pytest
from agent.context import Context
from infra.storage.classification_log_store import ClassificationLogStore
from skills.signal.same_day_dedup_gate import SameDayDedupGate


async def _log_fired(store, *, event_id, trader="stocktalkweekly",
                     ticker="SEI", side="long"):
    await store.insert(
        event_id=event_id, trader_handle=trader, msg_text="OPENING $SEI",
        features={}, llm_response=None, bucket="HIGH", confidence=1.0,
        size_pct=0.0, size_source="shortcut_stated", action_taken="fired",
        reason="stated_size_in_message", ticker=ticker, side=side,
    )


@pytest.mark.asyncio
async def test_has_fired_recently_excludes_current_event(db):
    store = ClassificationLogStore(db)
    await _log_fired(store, event_id="e1")
    # The event that just logged its own 'fired' row must NOT see itself.
    assert await store.has_fired_recently(
        trader_handle="stocktalkweekly", ticker="SEI", side="long",
        hours=24, exclude_event_id="e1") is False
    # A different, later event DOES see e1 as a prior fire.
    assert await store.has_fired_recently(
        trader_handle="stocktalkweekly", ticker="SEI", side="long",
        hours=24, exclude_event_id="e2") is True


@pytest.mark.asyncio
async def test_first_signal_fires_second_is_deduped(db):
    """Regression for fix/no-trades-may-8: the FIRST (trader,ticker,side)
    must fire; only an identical SECOND within the window is suppressed."""
    store = ClassificationLogStore(db)
    gate = SameDayDedupGate(store, window_hours=24)

    await _log_fired(store, event_id="e1")            # logger already ran
    ctx1 = Context(trace_id="t1", event_id="e1")
    ctx1.update({"bucket": "HIGH", "ticker": "SEI", "side": "long",
                 "trader_handle": "stocktalkweekly"})
    r1 = await gate.run(ctx1)
    assert r1.status == "success", "first-ever signal must NOT self-suppress"
    assert ctx1.get("bucket") == "HIGH"

    await _log_fired(store, event_id="e2")            # identical repost
    ctx2 = Context(trace_id="t2", event_id="e2")
    ctx2.update({"bucket": "HIGH", "ticker": "SEI", "side": "long",
                 "trader_handle": "stocktalkweekly"})
    r2 = await gate.run(ctx2)
    assert r2.status == "skip"
    assert ctx2.get("bucket") == "SKIP"
    assert ctx2.get("size_source") == "dedup"
