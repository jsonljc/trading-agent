from infra.bridge_client.discord_extension_forwarder import build_envelope, map_channel


CHANNEL_MAP = {
    "111": "mystic",
    "222": "yonezu",
    "333": "stock-talk-portfolio",
}


def test_map_channel_known():
    assert map_channel("111", CHANNEL_MAP) == "mystic"
    assert map_channel("222", CHANNEL_MAP) == "yonezu"


def test_map_channel_unknown_returns_none():
    assert map_channel("999", CHANNEL_MAP) is None


def test_map_channel_empty_id_returns_none():
    assert map_channel("", CHANNEL_MAP) is None
    assert map_channel(None, CHANNEL_MAP) is None


def test_build_envelope_shape():
    env = build_envelope(
        channel="mystic",
        author="Mystic",
        content="OPEN $SHEN — full multi-paragraph thesis goes here ...",
        message_id="987654321098765432",
        received_at="2026-04-28T20:00:00Z",
    )
    assert env["source"] == "discord_ext"
    assert env["channel"] == "mystic"
    assert env["author"] == "Mystic"
    assert env["trigger_preview"].startswith("OPEN $SHEN")
    assert env["received_at"] == "2026-04-28T20:00:00Z"
    # event_id must be deterministic from message_id so retries dedup naturally.
    assert env["event_id"] == "discord_ext:987654321098765432"


def test_build_envelope_received_at_defaults_to_now():
    env = build_envelope(
        channel="mystic", author="Mystic", content="x", message_id="1",
    )
    # Should be ISO-8601 UTC with 'Z' or '+00:00' suffix.
    assert env["received_at"].endswith("Z") or env["received_at"].endswith("+00:00")


def test_build_envelope_preserves_full_content():
    long_body = "a" * 3000
    env = build_envelope(
        channel="mystic", author="Mystic", content=long_body, message_id="1",
    )
    assert env["trigger_preview"] == long_body  # no truncation


import asyncio
import os
import tempfile
import json as _json


async def _accept_one_line(socket_path: str, out: list):
    server = await asyncio.start_unix_server(
        lambda r, w: _read_line(r, w, out), path=socket_path
    )
    async with server:
        await asyncio.sleep(0.5)


async def _read_line(reader, writer, out):
    data = await reader.readline()
    out.append(data.decode())
    writer.close()


async def test_bridge_socket_client_writes_one_line():
    from infra.bridge_client.discord_extension_forwarder import BridgeSocketClient

    fd, path = tempfile.mkstemp(suffix=".sock", dir="/tmp")
    os.close(fd)
    os.unlink(path)

    out: list[str] = []
    server_task = asyncio.create_task(_accept_one_line(path, out))
    await asyncio.sleep(0.1)

    client = BridgeSocketClient(path)
    await client.send({"event_id": "x", "source": "discord_ext", "channel": "mystic",
                        "author": "a", "trigger_preview": "p", "received_at": "t"})
    await asyncio.sleep(0.2)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass

    assert len(out) == 1
    parsed = _json.loads(out[0])
    assert parsed["channel"] == "mystic"
    assert out[0].endswith("\n")


async def test_bridge_socket_client_buffers_when_socket_missing(tmp_path):
    from infra.bridge_client.discord_extension_forwarder import BridgeSocketClient

    missing_path = str(tmp_path / "does_not_exist.sock")

    client = BridgeSocketClient(missing_path)
    # Should not raise; should buffer.
    await client.send({"event_id": "1", "source": "discord_ext", "channel": "mystic",
                        "author": "a", "trigger_preview": "p", "received_at": "t"})
    assert client.buffered_count() == 1
