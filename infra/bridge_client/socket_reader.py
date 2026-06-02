import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class TriggerEvent:
    event_id: str
    source: str
    channel: str
    author: str
    trigger_preview: str
    received_at: str


class SocketReader:
    """Reads newline-delimited JSON trigger events from a Unix domain socket.

    The Chrome extension is the SOLE capture path, so a malformed event is a real
    dropped trade signal — it must be made visible, not silently logged-and-lost.
    Parse failures are dead-lettered to a file and surfaced via an optional alert
    callback, and counted on `parse_error_count`.
    """

    def __init__(self, socket_path: str, *, deadletter_path: str | None = None,
                 on_parse_error=None) -> None:
        self._path = socket_path
        self._server: asyncio.Server | None = None
        self._deadletter_path = deadletter_path
        self._on_parse_error = on_parse_error  # async callable(raw: str, err: str)
        self.parse_error_count = 0

    async def start(self, on_event) -> None:
        """Start listening. Calls on_event(TriggerEvent) for each received event."""
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

        async def _connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._handle(reader, writer, on_event)

        self._server = await asyncio.start_unix_server(_connected, path=self._path)
        logger.info("Bridge socket listening at %s", self._path)
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, on_event) -> None:
        try:
            while not reader.at_eof():
                data = await reader.readline()
                if data:
                    try:
                        payload = json.loads(data.decode())
                        event = TriggerEvent(**payload)
                        await on_event(event)
                    except Exception as exc:
                        await self._dead_letter(data, exc)
        except Exception:
            logger.exception("Error reading from bridge connection")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dead_letter(self, raw: bytes, exc: Exception) -> None:
        """Persist + surface a malformed event instead of dropping it silently."""
        self.parse_error_count += 1
        try:
            decoded = raw.decode(errors="replace").rstrip("\n")
        except Exception:
            decoded = repr(raw)
        logger.error("Bridge event parse failure (#%d): %s — raw: %s",
                     self.parse_error_count, exc, decoded)
        if self._deadletter_path:
            try:
                line = json.dumps({
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(exc),
                    "raw": decoded,
                })
                with open(self._deadletter_path, "a") as f:
                    f.write(line + "\n")
            except Exception:
                logger.exception("Failed to write bridge dead-letter file")
        if self._on_parse_error is not None:
            try:
                await self._on_parse_error(decoded, str(exc))
            except Exception:
                logger.exception("Bridge parse-error alert failed")
