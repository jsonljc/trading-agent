import asyncio
import json
import os
import tempfile
import urllib.request
import urllib.error

from pathlib import Path

from infra.bridge_client.socket_reader import SocketReader, TriggerEvent
from infra.bridge_client.discord_extension_forwarder import run_forwarder


async def test_post_signal_arrives_as_trigger_event():
    fd, sock_path = tempfile.mkstemp(suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(sock_path)

    received: list[TriggerEvent] = []

    async def on_event(e: TriggerEvent):
        received.append(e)

    reader = SocketReader(sock_path)
    reader_task = asyncio.create_task(reader.start(on_event))
    await asyncio.sleep(0.1)

    channel_map = {"42": "mystic"}
    forwarder_task = asyncio.create_task(
        run_forwarder(host="127.0.0.1", port=9877,
                      socket_path=sock_path, channel_map=channel_map,
                      liveness_path=sock_path + ".liveness.json")
    )
    await asyncio.sleep(0.2)

    body = json.dumps({
        "channel_id": "42",
        "server_id": "server",
        "author": "Mystic",
        "content": "OPEN $SHEN " + "x" * 2000,
        "message_id": "msg-1",
        "timestamp": "2026-04-28T20:00:00.000Z",
    }).encode()

    def post():
        req = urllib.request.Request(
            "http://127.0.0.1:9877/signal", data=body,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status

    status = await asyncio.get_running_loop().run_in_executor(None, post)
    assert status == 204

    await asyncio.sleep(0.3)

    forwarder_task.cancel()
    reader_task.cancel()
    for t in (forwarder_task, reader_task):
        try:
            await t
        except asyncio.CancelledError:
            pass

    assert len(received) == 1
    ev = received[0]
    assert ev.channel == "mystic"
    assert ev.source == "discord_ext"
    assert ev.author == "Mystic"
    assert "OPEN $SHEN" in ev.trigger_preview
    assert len(ev.trigger_preview) > 2000  # full content preserved
    assert ev.event_id == "discord_ext:msg-1"


async def test_post_signal_unknown_channel_drops_with_204():
    fd, sock_path = tempfile.mkstemp(suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(sock_path)

    received: list[TriggerEvent] = []

    async def on_event(e: TriggerEvent):
        received.append(e)

    reader = SocketReader(sock_path)
    reader_task = asyncio.create_task(reader.start(on_event))
    await asyncio.sleep(0.1)

    forwarder_task = asyncio.create_task(
        run_forwarder(host="127.0.0.1", port=9878,
                      socket_path=sock_path, channel_map={"42": "mystic"},
                      liveness_path=sock_path + ".liveness.json")
    )
    await asyncio.sleep(0.2)

    body = json.dumps({
        "channel_id": "999", "server_id": "s", "author": "x",
        "content": "y", "message_id": "m", "timestamp": "t",
    }).encode()

    def post():
        req = urllib.request.Request(
            "http://127.0.0.1:9878/signal", data=body,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status

    status = await asyncio.get_running_loop().run_in_executor(None, post)
    assert status == 204
    await asyncio.sleep(0.2)

    forwarder_task.cancel()
    reader_task.cancel()
    for t in (forwarder_task, reader_task):
        try:
            await t
        except asyncio.CancelledError:
            pass

    assert received == []


async def test_beacon_and_signal_update_channel_liveness(tmp_path):
    """Both POST /beacon and POST /signal stamp the per-channel liveness file."""
    fd, sock_path = tempfile.mkstemp(suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(sock_path)

    liveness_path = tmp_path / "channel_liveness.json"
    channel_map = {"42": "mystic", "77": "wallstengine"}
    forwarder_task = asyncio.create_task(
        run_forwarder(host="127.0.0.1", port=9879,
                      socket_path=sock_path, channel_map=channel_map,
                      liveness_path=str(liveness_path))
    )
    await asyncio.sleep(0.2)

    # Seeded on startup: both tracked channels present, none stale yet.
    seeded = json.loads(liveness_path.read_text())
    assert set(seeded["tracked"]) == {"mystic", "wallstengine"}
    seed_ts = seeded["channels"]["mystic"]

    def post(path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:9879{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status

    # A beacon for mystic bumps only mystic's timestamp.
    await asyncio.sleep(0.01)
    status = await asyncio.get_running_loop().run_in_executor(
        None, post, "/beacon", {"channels": ["42"]})
    assert status == 204
    after_beacon = json.loads(liveness_path.read_text())
    assert after_beacon["channels"]["mystic"] != seed_ts          # refreshed
    assert after_beacon["channels"]["wallstengine"] == seed_ts     # untouched

    # A signal for wallstengine bumps wallstengine via the /signal path.
    status = await asyncio.get_running_loop().run_in_executor(
        None, post, "/signal",
        {"channel_id": "77", "author": "x", "content": "OPEN NVDA",
         "message_id": "m1", "timestamp": "2026-06-26T13:00:00Z"})
    assert status == 204
    after_signal = json.loads(liveness_path.read_text())
    assert after_signal["channels"]["wallstengine"] != seed_ts

    forwarder_task.cancel()
    try:
        await forwarder_task
    except asyncio.CancelledError:
        pass
