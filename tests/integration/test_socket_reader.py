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


async def test_malformed_event_is_dead_lettered_and_alerted():
    fd, socket_path = tempfile.mkstemp(suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(socket_path)
    fd2, dl_path = tempfile.mkstemp(suffix=".jsonl", dir="/tmp")
    os.close(fd2)
    os.unlink(dl_path)

    alerts: list[str] = []

    async def on_parse_error(raw: str, err: str) -> None:
        alerts.append(raw)

    reader = SocketReader(socket_path, deadletter_path=dl_path,
                          on_parse_error=on_parse_error)
    task = asyncio.create_task(reader.start(lambda e: asyncio.sleep(0)))
    await asyncio.sleep(0.1)

    _, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(b'{not valid json at all}\n')   # malformed -> must NOT vanish
    await writer.drain()
    writer.close()
    await asyncio.sleep(0.1)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert reader.parse_error_count == 1
    assert len(alerts) == 1
    with open(dl_path) as f:
        contents = f.read()
    assert "not valid json" in contents
    os.unlink(dl_path)
