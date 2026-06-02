import pytest
from unittest.mock import AsyncMock
from agent.heartbeat import Heartbeat


@pytest.mark.asyncio
async def test_disabled_when_url_none():
    hb = Heartbeat(None, interval_seconds=1, ping=AsyncMock())
    hb.start()
    assert hb._task is None          # nothing scheduled
    assert await hb.ping_once() is False


@pytest.mark.asyncio
async def test_ping_once_calls_url():
    ping = AsyncMock()
    hb = Heartbeat("https://hc.example/abc", interval_seconds=1, ping=ping)
    assert await hb.ping_once() is True
    ping.assert_awaited_once_with("https://hc.example/abc")


@pytest.mark.asyncio
async def test_ping_once_swallows_errors():
    # A failed ping must never crash the heartbeat loop.
    ping = AsyncMock(side_effect=RuntimeError("network down"))
    hb = Heartbeat("https://hc.example/abc", interval_seconds=1, ping=ping)
    assert await hb.ping_once() is False
