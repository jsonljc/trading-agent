from __future__ import annotations
import asyncio
import logging

logger = logging.getLogger(__name__)


async def _http_ping(url: str) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        await client.get(url)


class Heartbeat:
    """External dead-man's-switch ping (healthchecks.io-style).

    A crashed or slept bot otherwise looks identical to a quiet trading day. This
    periodically GETs a monitoring URL; if the pings stop, the external monitor
    alerts. Disabled (no-op) when no URL is configured.
    """

    def __init__(self, url: str | None, *, interval_seconds: int = 60, ping=None) -> None:
        self._url = url
        self._interval = interval_seconds
        self._ping = ping or _http_ping
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if not self._url:
            logger.info("Heartbeat disabled (no heartbeat_url configured)")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("Heartbeat started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopping:
            await self.ping_once()
            await asyncio.sleep(self._interval)

    async def ping_once(self) -> bool:
        if not self._url:
            return False
        try:
            await self._ping(self._url)
            return True
        except Exception as exc:
            logger.warning("Heartbeat ping failed: %s", exc)
            return False
