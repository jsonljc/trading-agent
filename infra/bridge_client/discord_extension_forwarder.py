"""HTTP-to-bridge-socket forwarder for the Discord browser extension.

Receives POSTs from a Chromium content script with full Discord message text
and forwards them to the agent's existing Unix-socket trigger pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
                try:
                    self._writer.close()
                except Exception:
                    pass
                self._writer = None  # next send retries

    async def close(self) -> None:
        async with self._lock:
            if self._writer is not None:
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except Exception:
                    pass
                self._writer = None


def _make_handler(channel_map: dict[str, str], client: BridgeSocketClient,
                  loop: asyncio.AbstractEventLoop):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.info("forwarder %s - " + fmt, self.address_string(), *args)

        def _respond(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            # CORS for the extension's fetch from the discord.com origin.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()

        def do_OPTIONS(self):
            self._respond(204)

        def do_POST(self):
            if self.path != "/signal":
                self._respond(404)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode())
            except Exception:
                logger.exception("Bad JSON from extension")
                self._respond(400)
                return

            channel = map_channel(payload.get("channel_id"), channel_map)
            if channel is None:
                logger.info("Dropping unmapped channel_id=%s", payload.get("channel_id"))
                self._respond(204)
                return

            envelope = build_envelope(
                channel=channel,
                author=str(payload.get("author", "unknown")),
                content=str(payload.get("content", "")),
                message_id=str(payload.get("message_id", "")),
                received_at=payload.get("timestamp"),
            )
            fut = asyncio.run_coroutine_threadsafe(client.send(envelope), loop)
            try:
                fut.result(timeout=1.0)
            except Exception:
                logger.exception("Failed to forward envelope")
                # Fix #2: cancel the orphaned coroutine so it doesn't pile up
                # holding the BridgeSocketClient lock under sustained backpressure.
                fut.cancel()
                self._respond(503)
                return
            self._respond(204)
    return Handler


async def run_forwarder(host: str, port: int, socket_path: str,
                        channel_map: dict[str, str]) -> None:
    """Run the HTTP forwarder until cancelled."""
    loop = asyncio.get_running_loop()
    client = BridgeSocketClient(socket_path)
    handler_cls = _make_handler(channel_map, client, loop)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    logger.info("Discord extension forwarder listening on %s:%s", host, port)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        # Run blocking shutdown off the event loop so in-flight handlers
        # awaiting client.send() via run_coroutine_threadsafe can complete.
        await loop.run_in_executor(None, httpd.shutdown)
        httpd.server_close()
        thread.join(timeout=2)
        await client.close()
