"""HTTP-to-bridge-socket forwarder for the Discord browser extension.

Receives POSTs from a Chromium content script with full Discord message text
and forwards them to the agent's existing Unix-socket trigger pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def map_channel(channel_id: Optional[str], channel_map: dict[str, str]) -> Optional[str]:
    """Return the canonical channel name for a Discord channel ID, or None."""
    if not channel_id:
        return None
    return channel_map.get(channel_id)


def build_envelope(channel: str, author: str, content: str, message_id: str,
                   received_at: Optional[str] = None) -> dict:
    """Build the bridge-socket envelope the agent's SocketReader expects."""
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "event_id": f"discord_ext:{message_id}",
        "source": "discord_ext",
        "channel": channel,
        "author": author,
        "trigger_preview": content,
        "received_at": received_at,
    }


MAX_BUFFER = 100


class BridgeSocketClient:
    """Connects to the agent's Unix socket and writes newline-JSON envelopes.

    Buffers up to MAX_BUFFER recent envelopes when the socket isn't reachable
    and flushes them on next successful connect.
    """

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._writer: Optional[asyncio.StreamWriter] = None
        self._buffer: deque[dict] = deque(maxlen=MAX_BUFFER)
        self._lock = asyncio.Lock()

    def buffered_count(self) -> int:
        return len(self._buffer)

    async def _connect(self) -> bool:
        try:
            _, writer = await asyncio.open_unix_connection(self._path)
            self._writer = writer
            return True
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            logger.warning("Bridge socket unreachable at %s: %s", self._path, e)
            self._writer = None
            return False

    async def send(self, envelope: dict) -> None:
        async with self._lock:
            self._buffer.append(envelope)
            if self._writer is None:
                if not await self._connect():
                    return  # remain buffered
            # Flush buffer in order.
            try:
                while self._buffer:
                    env = self._buffer[0]
                    line = (json.dumps(env) + "\n").encode()
                    self._writer.write(line)
                    await self._writer.drain()
                    self._buffer.popleft()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                logger.warning("Bridge socket write failed: %s", e)
                self._writer = None  # next send retries
