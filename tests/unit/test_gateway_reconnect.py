import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from infra.ib.gateway import IBGateway


def _paper_policy():
    p = MagicMock()
    p.ib_gateway.host = "127.0.0.1"
    p.ib_gateway.port = 7497
    p.ib_gateway.client_id = 1
    p.ib_gateway.mode = "paper"
    p.ib_gateway.paper_account_prefixes = ["DU"]
    return p


@pytest.mark.asyncio
async def test_reconnect_loop_retries_past_finite_list_until_success():
    """The old code gave up after 5 attempts. Regression test: the loop must
    keep trying indefinitely (10 attempts here, well past the old give-up)."""
    gw = IBGateway(_paper_policy())

    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        if attempts["n"] < 10:
            raise ConnectionError("refused")
        # 10th attempt succeeds

    gw.connect = fake_connect

    with patch("asyncio.sleep", new=AsyncMock()):
        await gw._reconnect_loop()

    assert attempts["n"] == 10


@pytest.mark.asyncio
async def test_reconnect_failing_callback_fires_at_5min_threshold():
    """on_reconnect_failing must fire once we've been failing for >= 5 minutes."""
    gw = IBGateway(_paper_policy())

    failing_alerts: list[int] = []

    async def on_failing(minutes: int) -> None:
        failing_alerts.append(minutes)

    gw._on_reconnect_failing = on_failing
    gw.connect = AsyncMock(side_effect=ConnectionError("refused"))

    # Fake clock: starts at 0, advances by 60s per call so the 5th read = 300s
    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    async def fake_sleep(_):
        clock["t"] += 60.0
        # Stop the loop once we've crossed 5 minutes by cancelling the connect
        # mock (next iteration will pick up the cancellation).
        if clock["t"] > 360.0:
            raise asyncio.CancelledError()

    with patch("infra.ib.gateway.time.monotonic", fake_monotonic), \
         patch("asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await gw._reconnect_loop()

    assert 5 in failing_alerts


@pytest.mark.asyncio
async def test_reconnect_failing_thresholds_do_not_double_fire():
    """Each threshold (5, 15, 30, 60) fires at most once even if many loop
    iterations land in the same bucket."""
    gw = IBGateway(_paper_policy())

    failing_alerts: list[int] = []

    async def on_failing(minutes: int) -> None:
        failing_alerts.append(minutes)

    gw._on_reconnect_failing = on_failing
    gw.connect = AsyncMock(side_effect=ConnectionError("refused"))

    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    async def fake_sleep(_):
        # Tiny advance so we generate many iterations inside the 5-minute window
        clock["t"] += 30.0
        if clock["t"] > 600.0:  # 10 minutes
            raise asyncio.CancelledError()

    with patch("infra.ib.gateway.time.monotonic", fake_monotonic), \
         patch("asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await gw._reconnect_loop()

    # 5-min threshold fires exactly once even though many iterations sit above 5min.
    assert failing_alerts.count(5) == 1


@pytest.mark.asyncio
async def test_on_reconnect_fires_when_connect_eventually_succeeds():
    gw = IBGateway(_paper_policy())

    reconnect_calls = {"n": 0}

    async def on_reconnect():
        reconnect_calls["n"] += 1

    gw._on_reconnect = on_reconnect

    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("refused")

    gw.connect = fake_connect

    with patch("asyncio.sleep", new=AsyncMock()):
        await gw._reconnect_loop()

    assert reconnect_calls["n"] == 1
