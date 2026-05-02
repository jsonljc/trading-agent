import pytest
from agent.context import Context
from agent.traders.profile import TraderProfile, ConvictionExample
from agent.traders.registry import TraderRegistry
from skills.signal.trader_router import TraderRouter


def make_profile(handle: str, author: str, bot_skip: tuple[str, ...] = ()) -> TraderProfile:
    return TraderProfile(
        handle=handle, display_name=author, discord_author_pattern=author,
        alert_mention=f"@{author} - Alerts", require_alert_mention=True,
        bot_authors_to_skip=bot_skip, auto_execute=True,
        size_in_message=False, prefer_message_size=True,
        classifier_model="claude-haiku-4-5", availability_phrases=(),
        conviction_examples=(ConvictionExample(msg="x", bucket="LOW", why="y"),),
    )


@pytest.mark.asyncio
async def test_router_attaches_matching_profile_to_ctx():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine")])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine",
        "full_message_text": "OPEN $X @Wall St Engine - Alerts",
    })

    result = await router.run(ctx)

    assert result.status == "success"
    assert ctx.get("trader_handle") == "wse"
    assert ctx.get("trader_auto_execute") is True


@pytest.mark.asyncio
async def test_router_skips_when_no_matching_profile():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine")])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={"author": "Random Person", "full_message_text": "buy AAPL"})

    result = await router.run(ctx)

    assert result.status == "skip"
    assert "no_trader_profile" in (result.reason or "")


@pytest.mark.asyncio
async def test_router_skips_bot_authors():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine", bot_skip=("WSE",))])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={"author": "WSE", "full_message_text": "FDA APPROVES X"})

    result = await router.run(ctx)
    assert result.status == "skip"
    assert "bot_author" in (result.reason or "")


@pytest.mark.asyncio
async def test_router_skips_when_alert_mention_required_but_missing():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine")])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine",
        "full_message_text": "OPEN $X — quick note no mention",
    })

    result = await router.run(ctx)
    assert result.status == "skip"
    assert "missing_alert_mention" in (result.reason or "")
