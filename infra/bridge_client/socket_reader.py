import asyncio
import json
import logging
import os
from dataclasses import dataclass

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
    """Reads newline-delimited JSON trigger events from a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._server: asyncio.Server | None = None

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
                    except Exception:
                        logger.exception("Error parsing bridge event")
        except Exception:
            logger.exception("Error reading from bridge connection")
        finally:
            writer.close()
            await writer.wait_closed()
