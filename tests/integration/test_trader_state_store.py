import pytest
from datetime import datetime, timezone, timedelta
from infra.storage.trader_state_store import TraderStateStore


@pytest.mark.asyncio
async def test_set_and_get_unavailable_until(db):
    store = TraderStateStore(db)
    until = datetime.now(timezone.utc) + timedelta(days=7)
    await store.set_unavailable_until(handle="mystic", until=until)
    got = await store.get_unavailable_until("mystic")
    assert got is not None
    assert abs((got - until).total_seconds()) < 1


@pytest.mark.asyncio
async def test_get_returns_none_when_no_state(db):
    store = TraderStateStore(db)
    assert await store.get_unavailable_until("nobody") is None
