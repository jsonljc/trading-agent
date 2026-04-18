import asyncio
import json
import os
import tempfile
import pytest
from infra.bridge_client.socket_reader import SocketReader, TriggerEvent


async def test_socket_reader_receives_event():
    # tmp_path paths are too long for AF_UNIX on macOS (104-char limit);
    # use a short path in /tmp instead.
    fd, socket_path = tempfile.mkstemp(suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(socket_path)  # socket_reader creates the file via bind
    received: list[TriggerEvent] = []

    async def on_event(e: TriggerEvent):
        received.append(e)

    reader = SocketReader(socket_path)
    task = asyncio.create_task(reader.start(on_event))

    await asyncio.sleep(0.1)  # let server start

    payload = {
        "event_id": "e1", "source": "discord_notification",
        "channel": "mystic", "author": "Mystic",
        "trigger_preview": "Long $AVEX", "received_at": "2026-04-18T10:00:00Z",
    }
    _, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(json.dumps(payload).encode() + b"\n")
    await writer.drain()
    writer.close()

    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(received) == 1
    assert received[0].channel == "mystic"
    assert received[0].trigger_preview == "Long $AVEX"
