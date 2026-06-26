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
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# infra/bridge_client/discord_extension_forwarder.py -> repo root
REPO_DIR = Path(__file__).resolve().parents[2]
# Per-channel liveness hand-off file. Written here, read by bin/agent-watchdog
# (a separate launchd process) so it can alert when a tracked channel's capture
# goes silent. Kept self-describing so the watchdog needs no policy access.
DEFAULT_LIVENESS_PATH = REPO_DIR / "data" / "channel_liveness.json"


def _utc_now_iso() -> str:
    """ISO-8601 UTC with a trailing 'Z' (matches the bridge envelope format)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def map_channel(channel_id: Optional[str], channel_map: dict[str, str]) -> Optional[str]:
    """Return the canonical channel name for a Discord channel ID, or None."""
    if not channel_id:
        return None
    return channel_map.get(channel_id)


def build_envelope(channel: str, author: str, content: str, message_id: str,
                   received_at: Optional[str] = None) -> dict:
    """Build the bridge-socket envelope the agent's SocketReader expects."""
    if received_at is None:
        received_at = _utc_now_iso()
    return {
        "event_id": f"discord_ext:{message_id}",
        "source": "discord_ext",
        "channel": channel,
        "author": author,
        "trigger_preview": content,
        "received_at": received_at,
    }


def beacon_channel_ids(payload: object) -> list[str]:
    """Extract the watched Discord channel IDs from a /beacon POST body.

    Each Discord tab's content script periodically reports the channel it is
    actively watching. Accepts a "channels"/"watching" list or a singular
    "channel_id"; coerces every entry to a string (Discord IDs are numeric).
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("channels")
    if raw is None:
        raw = payload.get("watching")
    if raw is None:
        single = payload.get("channel_id")
        raw = [single] if single is not None else []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(c) for c in raw if c is not None and str(c) != ""]


class ChannelLivenessStore:
    """Records the last time each TRACKED channel was observed alive.

    The Discord extension cannot prove it is still capturing merely by staying
    open: its MutationObserver can be silently orphaned when Discord re-mounts
    the message list (see extension/content.js). So the extension emits a
    periodic per-channel beacon AND every captured signal stamps its channel.
    We persist the most-recent timestamp per channel to a JSON file that
    bin/agent-watchdog reads from a separate process to alert when any tracked
    channel goes silent — distinguishing "trader went quiet" (beacon still
    fresh) from "that channel's capture died" (beacon stale).

    File shape (self-describing so the watchdog needs no policy access)::

        {
          "tracked":   ["mystic", "wallstengine", ...],   # all mapped names
          "channels":  {"mystic": "<iso8601 Z>", ...},    # last-seen per name
          "updated_at": "<iso8601 Z>"
        }
    """

    def __init__(self, channel_map: dict[str, str],
                 path: "str | Path" = DEFAULT_LIVENESS_PATH) -> None:
        self._channel_map = dict(channel_map)
        # Roster of canonical names, de-duped, insertion order preserved.
        self._tracked = list(dict.fromkeys(channel_map.values()))
        self._channels: dict[str, str] = {}
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def tracked(self) -> list[str]:
        return list(self._tracked)

    def seed(self, now_iso: str) -> None:
        """Stamp every tracked channel at startup (optimistic cold-start).

        Seeding avoids a spurious "stale" verdict during the first beacon
        interval, while a channel whose beacon never arrives still ages past
        the watchdog's staleness threshold and fires.
        """
        with self._lock:
            for name in self._tracked:
                self._channels.setdefault(name, now_iso)
            self._flush_locked(now_iso)

    def record_ids(self, channel_ids: Optional[Iterable[str]], now_iso: str) -> None:
        """Stamp last-seen=now for each mapped (tracked) channel ID. No-op for
        unmapped IDs or an empty/None input."""
        if not channel_ids:
            return
        with self._lock:
            changed = False
            for cid in channel_ids:
                if cid is None:
                    continue
                name = self._channel_map.get(str(cid))
                if name is None:
                    continue  # untracked channel — ignore
                self._channels[name] = now_iso
                changed = True
            if changed:
                self._flush_locked(now_iso)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "tracked": list(self._tracked),
                "channels": dict(self._channels),
            }

    def _flush_locked(self, now_iso: str) -> None:
        payload = {
            "tracked": list(self._tracked),
            "channels": dict(self._channels),
            "updated_at": now_iso,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._path)  # atomic rename so readers never see a partial write
        except OSError as e:
            logger.warning("Failed to write channel liveness file %s: %s", self._path, e)


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
                  loop: asyncio.AbstractEventLoop,
                  liveness: "ChannelLivenessStore | None" = None):
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

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            return json.loads(raw.decode())

        def _handle_beacon(self):
            # Liveness-only ping: stamp the watched channels and ack. Never
            # touches the bridge socket, so it stays cheap even under backpressure.
            try:
                payload = self._read_json()
            except Exception:
                logger.exception("Bad JSON from extension beacon")
                self._respond(400)
                return
            if liveness is not None:
                liveness.record_ids(beacon_channel_ids(payload), _utc_now_iso())
            self._respond(204)

        def do_POST(self):
            if self.path == "/beacon":
                self._handle_beacon()
                return
            if self.path != "/signal":
                self._respond(404)
                return
            try:
                payload = self._read_json()
            except Exception:
                logger.exception("Bad JSON from extension")
                self._respond(400)
                return

            channel = map_channel(payload.get("channel_id"), channel_map)
            if channel is None:
                logger.info("Dropping unmapped channel_id=%s", payload.get("channel_id"))
                self._respond(204)
                return

            # A captured signal also proves this channel's capture is alive —
            # stamp liveness on receipt, independent of bridge-socket health.
            if liveness is not None:
                liveness.record_ids([payload.get("channel_id")], _utc_now_iso())

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
                        channel_map: dict[str, str],
                        liveness_path: "str | Path" = DEFAULT_LIVENESS_PATH) -> None:
    """Run the HTTP forwarder until cancelled."""
    loop = asyncio.get_running_loop()
    client = BridgeSocketClient(socket_path)
    liveness = ChannelLivenessStore(channel_map, path=liveness_path)
    liveness.seed(_utc_now_iso())
    handler_cls = _make_handler(channel_map, client, loop, liveness)
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


def main() -> None:
    import argparse
    from agent.policy import load_policy

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="config/policy.yaml")
    parser.add_argument("--socket", default="/tmp/trading_bridge.sock")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    policy = load_policy(args.policy)
    cfg = policy.discord_extension

    asyncio.run(run_forwarder(
        host=args.host,
        port=cfg.forwarder_port,
        socket_path=args.socket,
        channel_map=cfg.channel_id_map,
    ))


if __name__ == "__main__":
    main()
