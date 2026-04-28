# Discord Browser Extension — Full-Text Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture full (untruncated) Discord message text for the three priority channels (`mystic`, `yonezu`, `stock-talk-portfolio`) and forward each message to the existing agent's bridge socket as a normal trigger event.

**Architecture:** A Chromium extension content script observes Discord's web client DOM via MutationObserver, extracts each new message's full text, and POSTs to a localhost Python forwarder. The forwarder maps the Discord channel ID to a canonical channel name and writes a `discord_ext`-sourced envelope to `/tmp/trading_bridge.sock` — the same envelope shape the agent's existing `SocketReader` already consumes. No agent changes; the agent's existing `IdempotencyStore` handles dedup against the parallel macOS notif-DB poller.

**Tech Stack:** Chromium Manifest V3 extension (vanilla JS, no build step), Python 3.11+ stdlib `http.server` for the forwarder, existing `agent/policy.py` (pydantic) for config, existing `infra/bridge_client/socket_reader.py` envelope shape.

**Spec:** `docs/superpowers/specs/2026-04-28-discord-browser-extension-capture-design.md`

---

## File Structure

**New files:**
- `extension/manifest.json` — MV3 manifest, host_permissions for `https://discord.com/*`
- `extension/content.js` — Content script: snapshot + MutationObserver + POST
- `extension/extract.js` — Pure DOM extraction function (separated for testability)
- `extension/icons/icon-128.png` — Placeholder icon
- `extension/test/harness.html` — Static harness with saved Discord DOM snippets for manual selector verification
- `infra/bridge_client/discord_extension_forwarder.py` — Python HTTP → Unix-socket bridge
- `tests/unit/test_discord_extension_forwarder.py` — Unit tests for channel mapping & envelope build
- `tests/integration/test_discord_extension_forwarder.py` — End-to-end forwarder integration test
- `docs/ops/discord-extension-setup.md` — One-page operator setup guide

**Modified files:**
- `agent/policy.py` — Add `DiscordExtensionConfig` pydantic model and field on `PolicyModel`
- `config/policy.yaml` — Add `discord_extension:` block with placeholder channel IDs
- `tests/unit/test_policy.py` (if it exists; otherwise a new test) — Verify the new config loads

**Unchanged (deliberate):**
- `infra/bridge_client/socket_reader.py` — Forwarder writes the envelope it already accepts
- `bridge/Sources/...` Swift code — Spec adopted the "no bridge change" alternative
- `agent/orchestrator.py`, `main.py` — No agent code changes

---

## Task 1: Add `discord_extension` config to policy schema

**Files:**
- Modify: `agent/policy.py`
- Modify: `config/policy.yaml`
- Create: `tests/unit/test_discord_extension_policy.py`

- [ ] **Step 1: Write failing test for policy parse**

Create `tests/unit/test_discord_extension_policy.py`:

```python
import textwrap
import tempfile
import os
from agent.policy import load_policy


BASE_POLICY = open("config/policy.yaml").read()


def _write(extra_yaml: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(path, "w") as f:
        f.write(BASE_POLICY)
        f.write("\n")
        f.write(extra_yaml)
    return path


def test_discord_extension_config_loads():
    path = _write(textwrap.dedent("""
        discord_extension:
          forwarder_port: 9876
          channel_id_map:
            "111111111111111111": mystic
            "222222222222222222": yonezu
            "333333333333333333": stock-talk-portfolio
    """))
    try:
        # If config/policy.yaml already contains discord_extension, the second
        # block is shadowed; that's fine — what we're testing is schema acceptance.
        policy = load_policy(path)
        assert policy.discord_extension.forwarder_port == 9876
        assert policy.discord_extension.channel_id_map["111111111111111111"] == "mystic"
    finally:
        os.unlink(path)


def test_discord_extension_config_optional():
    """Policy must still parse if the discord_extension block is absent."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    # Minimal policy without the new block — copy the existing file as-is and
    # rely on the default factory. The existing config/policy.yaml is the
    # baseline, and its absence of discord_extension proves backward compat.
    with open(path, "w") as f:
        f.write(BASE_POLICY)
    try:
        policy = load_policy(path)
        assert policy.discord_extension is not None
        assert policy.discord_extension.channel_id_map == {} or \
            isinstance(policy.discord_extension.channel_id_map, dict)
    finally:
        os.unlink(path)
```

- [ ] **Step 2: Run test, expect failure**

```
pytest tests/unit/test_discord_extension_policy.py -v
```

Expected: FAIL — `AttributeError: 'PolicyModel' object has no attribute 'discord_extension'` or pydantic validation error.

- [ ] **Step 3: Add `DiscordExtensionConfig` model and field**

Edit `agent/policy.py`. After the existing `ExecutionPolicy` class (around line 97), add:

```python
class DiscordExtensionConfig(BaseModel):
    forwarder_port: int = 9876
    channel_id_map: dict[str, str] = {}
```

Then in the `PolicyModel` class body (around line 114), add as the last field:

```python
    discord_extension: DiscordExtensionConfig = DiscordExtensionConfig()
```

- [ ] **Step 4: Add the config block to `config/policy.yaml`**

Append to `config/policy.yaml`:

```yaml

discord_extension:
  forwarder_port: 9876
  channel_id_map:
    # Replace with real Discord channel IDs (right-click channel → Copy ID
    # in Discord with Developer Mode enabled).
    "REPLACE_MYSTIC_CHANNEL_ID": mystic
    "REPLACE_YONEZU_CHANNEL_ID": yonezu
    "REPLACE_STOCK_TALK_PORTFOLIO_CHANNEL_ID": stock-talk-portfolio
```

- [ ] **Step 5: Run tests, expect pass**

```
pytest tests/unit/test_discord_extension_policy.py -v
```

Expected: 2 passed.

Also re-run the full unit suite to confirm no regression:

```
pytest tests/unit -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```
git add agent/policy.py config/policy.yaml tests/unit/test_discord_extension_policy.py
git commit -m "feat(policy): add discord_extension config block"
```

---

## Task 2: Forwarder — channel mapping pure function

**Files:**
- Create: `infra/bridge_client/discord_extension_forwarder.py`
- Create: `tests/unit/test_discord_extension_forwarder.py`

- [ ] **Step 1: Write failing test for `map_channel`**

Create `tests/unit/test_discord_extension_forwarder.py`:

```python
import pytest
from infra.bridge_client.discord_extension_forwarder import (
    map_channel,
    build_envelope,
)


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
```

- [ ] **Step 2: Run test, expect import failure**

```
pytest tests/unit/test_discord_extension_forwarder.py -v
```

Expected: FAIL — `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 3: Create the forwarder module skeleton with `map_channel`**

Create `infra/bridge_client/discord_extension_forwarder.py`:

```python
"""HTTP-to-bridge-socket forwarder for the Discord browser extension.

Receives POSTs from a Chromium content script with full Discord message text
and forwards them to the agent's existing Unix-socket trigger pipeline.
"""
from __future__ import annotations

import json
import logging
import uuid
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
    raise NotImplementedError  # filled in by Task 3
```

- [ ] **Step 4: Run mapping tests, expect pass; envelope test still failing**

```
pytest tests/unit/test_discord_extension_forwarder.py::test_map_channel_known -v
pytest tests/unit/test_discord_extension_forwarder.py::test_map_channel_unknown_returns_none -v
pytest tests/unit/test_discord_extension_forwarder.py::test_map_channel_empty_id_returns_none -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add infra/bridge_client/discord_extension_forwarder.py tests/unit/test_discord_extension_forwarder.py
git commit -m "feat(forwarder): add Discord extension forwarder channel mapping"
```

---

## Task 3: Forwarder — envelope builder

**Files:**
- Modify: `infra/bridge_client/discord_extension_forwarder.py`
- Modify: `tests/unit/test_discord_extension_forwarder.py`

- [ ] **Step 1: Add failing tests for `build_envelope`**

Append to `tests/unit/test_discord_extension_forwarder.py`:

```python
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
```

- [ ] **Step 2: Run, expect 3 failures (NotImplementedError)**

```
pytest tests/unit/test_discord_extension_forwarder.py -v
```

Expected: 3 new tests fail with `NotImplementedError`.

- [ ] **Step 3: Implement `build_envelope`**

In `infra/bridge_client/discord_extension_forwarder.py`, replace the `build_envelope` body:

```python
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
```

- [ ] **Step 4: Run tests, expect all pass**

```
pytest tests/unit/test_discord_extension_forwarder.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```
git add infra/bridge_client/discord_extension_forwarder.py tests/unit/test_discord_extension_forwarder.py
git commit -m "feat(forwarder): add envelope builder for Discord extension events"
```

---

## Task 4: Forwarder — Unix-socket client with reconnect

**Files:**
- Modify: `infra/bridge_client/discord_extension_forwarder.py`
- Modify: `tests/unit/test_discord_extension_forwarder.py`

- [ ] **Step 1: Write failing tests for `BridgeSocketClient`**

Append to `tests/unit/test_discord_extension_forwarder.py`:

```python
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

    missing_path = "/tmp/does_not_exist_yet_xyz.sock"
    try:
        os.unlink(missing_path)
    except FileNotFoundError:
        pass

    client = BridgeSocketClient(missing_path)
    # Should not raise; should buffer.
    await client.send({"event_id": "1", "source": "discord_ext", "channel": "mystic",
                        "author": "a", "trigger_preview": "p", "received_at": "t"})
    assert client.buffered_count() == 1
```

- [ ] **Step 2: Run, expect ImportError on `BridgeSocketClient`**

```
pytest tests/unit/test_discord_extension_forwarder.py -v
```

Expected: 2 new tests fail with ImportError.

- [ ] **Step 3: Implement `BridgeSocketClient`**

Append to `infra/bridge_client/discord_extension_forwarder.py`:

```python
import asyncio
from collections import deque

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
```

- [ ] **Step 4: Run tests, expect all pass**

```
pytest tests/unit/test_discord_extension_forwarder.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```
git add infra/bridge_client/discord_extension_forwarder.py tests/unit/test_discord_extension_forwarder.py
git commit -m "feat(forwarder): bridge-socket client with reconnect and buffering"
```

---

## Task 5: Forwarder — HTTP server endpoint

**Files:**
- Modify: `infra/bridge_client/discord_extension_forwarder.py`
- Create: `tests/integration/test_discord_extension_forwarder.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/integration/test_discord_extension_forwarder.py`:

```python
import asyncio
import json
import os
import tempfile
import urllib.request
import urllib.error

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
                      socket_path=sock_path, channel_map=channel_map)
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
                      socket_path=sock_path, channel_map={"42": "mystic"})
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
```

- [ ] **Step 2: Run, expect ImportError on `run_forwarder`**

```
pytest tests/integration/test_discord_extension_forwarder.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `run_forwarder` and HTTP handler**

Use the Python stdlib (no new dependencies — `aiohttp` is not in `pyproject.toml` and we don't want to add it for this). Append to `infra/bridge_client/discord_extension_forwarder.py`:

```python
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


def _make_handler(channel_map: dict[str, str], client: "BridgeSocketClient",
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
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
```

- [ ] **Step 4: Run integration tests, expect pass**

```
pytest tests/integration/test_discord_extension_forwarder.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```
git add infra/bridge_client/discord_extension_forwarder.py tests/integration/test_discord_extension_forwarder.py
git commit -m "feat(forwarder): HTTP /signal endpoint forwards to bridge socket"
```

---

## Task 6: Forwarder — `__main__` entry point

**Files:**
- Modify: `infra/bridge_client/discord_extension_forwarder.py`

- [ ] **Step 1: Add CLI entrypoint**

Append to `infra/bridge_client/discord_extension_forwarder.py`:

```python
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
```

- [ ] **Step 2: Smoke-test the CLI starts and exits cleanly**

```
.venv/bin/python -c "from infra.bridge_client.discord_extension_forwarder import main; print('importable')"
```

Expected: prints `importable`.

```
timeout 2 .venv/bin/python -m infra.bridge_client.discord_extension_forwarder --policy config/policy.yaml || true
```

Expected: starts, logs "listening on 127.0.0.1:9876", killed by timeout. No traceback.

- [ ] **Step 3: Commit**

```
git add infra/bridge_client/discord_extension_forwarder.py
git commit -m "feat(forwarder): add CLI entrypoint for Discord extension forwarder"
```

---

## Task 7: Extension — extraction module

**Files:**
- Create: `extension/extract.js`
- Create: `extension/test/harness.html`

The extension uses no build step. Selector logic is isolated in `extract.js` so it can be loaded in a static HTML harness for manual verification (no Jest infrastructure added). The harness is the test.

- [ ] **Step 1: Create `extension/extract.js`**

```javascript
// Pure DOM extraction. Given a Discord message <li> element, returns a
// {message_id, author, content, timestamp} object, or null if the element
// doesn't look like a renderable message.
//
// Selectors are attribute-based ([id^=...], [class*=...]) because Discord
// rotates class hashes on every release.
(function (root) {
  function extractMessage(el) {
    if (!el || !el.id || !el.id.startsWith("chat-messages-")) return null;

    // chat-messages-<channelId>-<messageId>
    const parts = el.id.split("-");
    const message_id = parts[parts.length - 1];
    if (!message_id) return null;

    const contentEl = el.querySelector('[id^="message-content-"]');
    const content = contentEl ? contentEl.innerText : "";

    const usernameEl = el.querySelector('[class*="username"]');
    const author = usernameEl ? usernameEl.innerText.trim() : "";

    const timeEl = el.querySelector("time");
    const timestamp = timeEl ? timeEl.getAttribute("datetime") : "";

    return { message_id, author, content, timestamp };
  }

  function channelIdFromUrl(url) {
    // https://discord.com/channels/<server_id>/<channel_id>
    const m = url.match(/\/channels\/(\d+)\/(\d+)/);
    if (!m) return { server_id: "", channel_id: "" };
    return { server_id: m[1], channel_id: m[2] };
  }

  root.DiscordExtract = { extractMessage, channelIdFromUrl };
})(typeof window !== "undefined" ? window : globalThis);
```

- [ ] **Step 2: Create `extension/test/harness.html` with saved Discord DOM snippets**

```html
<!doctype html>
<meta charset="utf-8">
<title>Discord Extract Harness</title>
<script src="../extract.js"></script>
<style>
  body { font-family: monospace; padding: 1em; }
  .case { border: 1px solid #ccc; padding: 0.5em; margin: 0.5em 0; }
  .pass { color: green; } .fail { color: red; font-weight: bold; }
</style>
<h1>Discord extraction tests</h1>
<div id="out"></div>

<!-- Saved Discord message DOM. Re-capture from a real Discord page if Discord
     redesigns the selectors. -->
<template id="case-text">
  <li id="chat-messages-111111-987654321098765432">
    <div>
      <span class="username-h_Y3Us">Mystic</span>
      <time datetime="2026-04-28T20:00:00.000Z">8:00 PM</time>
      <div id="message-content-987654321098765432">OPEN $SHEN - long thesis here</div>
    </div>
  </li>
</template>

<template id="case-empty">
  <li id="chat-messages-111111-1">
    <div>
      <span class="username-x">User</span>
      <time datetime="2026-04-28T20:00:00.000Z">x</time>
      <div id="message-content-1"></div>
    </div>
  </li>
</template>

<template id="case-not-message">
  <li id="not-a-message">irrelevant</li>
</template>

<script>
  function check(name, cond) {
    const div = document.createElement("div");
    div.className = "case " + (cond ? "pass" : "fail");
    div.textContent = (cond ? "PASS  " : "FAIL  ") + name;
    document.getElementById("out").appendChild(div);
  }
  function load(id) {
    return document.getElementById(id).content.firstElementChild.cloneNode(true);
  }

  const a = DiscordExtract.extractMessage(load("case-text"));
  check("text message id", a.message_id === "987654321098765432");
  check("text message author", a.author === "Mystic");
  check("text message content", a.content === "OPEN $SHEN - long thesis here");
  check("text message timestamp", a.timestamp === "2026-04-28T20:00:00.000Z");

  const b = DiscordExtract.extractMessage(load("case-empty"));
  check("empty content returns empty string", b.content === "");

  const c = DiscordExtract.extractMessage(load("case-not-message"));
  check("non-message element returns null", c === null);

  const d = DiscordExtract.channelIdFromUrl(
    "https://discord.com/channels/123/456");
  check("url parse channel_id", d.channel_id === "456" && d.server_id === "123");
</script>
```

- [ ] **Step 3: Verify harness in a browser**

Open `extension/test/harness.html` in Chrome. All six rows must show `PASS` in green. If any fail, fix `extract.js` and reload.

- [ ] **Step 4: Commit**

```
git add extension/extract.js extension/test/harness.html
git commit -m "feat(extension): add Discord message DOM extraction module + harness"
```

---

## Task 8: Extension — manifest and content script

**Files:**
- Create: `extension/manifest.json`
- Create: `extension/content.js`
- Create: `extension/icons/icon-128.png`

- [ ] **Step 1: Create `extension/manifest.json`**

```json
{
  "manifest_version": 3,
  "name": "Trading Agent — Discord Capture",
  "version": "0.1.0",
  "description": "Captures full message text from priority Discord channels and forwards to the local trading agent.",
  "host_permissions": [
    "https://discord.com/*",
    "http://localhost:9876/*",
    "http://127.0.0.1:9876/*"
  ],
  "content_scripts": [
    {
      "matches": ["https://discord.com/channels/*"],
      "js": ["extract.js", "content.js"],
      "run_at": "document_idle"
    }
  ],
  "icons": {
    "128": "icons/icon-128.png"
  }
}
```

- [ ] **Step 2: Create a placeholder icon**

```
mkdir -p extension/icons
# 1x1 transparent PNG, base64-decoded
python3 -c "import base64,pathlib; pathlib.Path('extension/icons/icon-128.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='))"
```

- [ ] **Step 3: Create `extension/content.js`**

```javascript
// Discord priority-channel full-text capture.
//
// Strategy:
//   1. Wait for the messages list to mount (Discord lazily loads it).
//   2. Snapshot existing visible messages into a "seen" set so we don't
//      emit the channel's history on first load.
//   3. MutationObserver fires for each new message; we extract and POST.

(function () {
  const FORWARDER_URL = "http://localhost:9876/signal";
  const MOUNT_TIMEOUT_MS = 30000;
  const POLL_INTERVAL_MS = 250;
  const POST_RETRIES = 3;
  const POST_BACKOFF_MS = 500;

  function log(...args) { console.log("[trading-agent-ext]", ...args); }
  function warn(...args) { console.warn("[trading-agent-ext]", ...args); }

  async function waitForMessagesList(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      // The messages-list container has a stable data-list-id under React.
      // Fall back to scanning for any chat-messages-* element's parent.
      const probe = document.querySelector('[id^="chat-messages-"]');
      if (probe && probe.parentElement) return probe.parentElement;
      await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
    }
    return null;
  }

  async function postSignal(payload) {
    let lastErr = null;
    for (let attempt = 0; attempt < POST_RETRIES; attempt++) {
      try {
        const resp = await fetch(FORWARDER_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          mode: "cors",
        });
        if (resp.ok || resp.status === 204) return true;
        lastErr = new Error("HTTP " + resp.status);
      } catch (e) {
        lastErr = e;
      }
      await new Promise(r => setTimeout(r, POST_BACKOFF_MS));
    }
    warn("Failed to forward signal after retries:", lastErr);
    return false;
  }

  async function init() {
    const { server_id, channel_id } = DiscordExtract.channelIdFromUrl(
      window.location.href);
    if (!channel_id) { log("not on a channel URL; bailing"); return; }

    const container = await waitForMessagesList(MOUNT_TIMEOUT_MS);
    if (!container) { warn("messages list never mounted"); return; }

    log("attached to messages list for channel", channel_id);

    const seen = new Set();
    container.querySelectorAll('[id^="chat-messages-"]').forEach(el => {
      seen.add(el.id);
    });
    log("snapshot:", seen.size, "existing messages");

    const observer = new MutationObserver(mutations => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (!(node instanceof Element)) continue;
          // Added node may be the message itself or a wrapper containing one.
          const candidates = node.id && node.id.startsWith("chat-messages-")
            ? [node]
            : Array.from(node.querySelectorAll('[id^="chat-messages-"]'));
          for (const el of candidates) {
            if (seen.has(el.id)) continue;
            seen.add(el.id);
            const extracted = DiscordExtract.extractMessage(el);
            if (!extracted) continue;
            const payload = { ...extracted, channel_id, server_id };
            postSignal(payload).then(ok => {
              if (ok) log("forwarded", extracted.message_id, extracted.content.slice(0, 60));
            });
          }
        }
      }
    });
    observer.observe(container, { childList: true, subtree: true });
  }

  // Re-run on URL change (Discord is a SPA — channel switches don't reload).
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      log("url changed; re-initialising");
      init();
    }
  }, 1000);

  init();
})();
```

- [ ] **Step 4: Load unpacked in Chrome and verify**

1. Open `chrome://extensions`, enable Developer Mode, click "Load unpacked", select the `extension/` directory.
2. Open a Discord channel URL.
3. Open DevTools console for that tab. You should see:
   - `[trading-agent-ext] attached to messages list for channel ...`
   - `[trading-agent-ext] snapshot: N existing messages`
4. Without the forwarder running, post a test message to the channel. Console should show `Failed to forward signal after retries:` (expected — forwarder is offline).

This is a manual gate; record the result in the commit message.

- [ ] **Step 5: Commit**

```
git add extension/manifest.json extension/content.js extension/icons/icon-128.png
git commit -m "feat(extension): manifest + content script with MutationObserver capture"
```

---

## Task 9: End-to-end manual integration test

**Files:** none new — verifies the assembled system.

- [ ] **Step 1: Fill in real channel IDs**

In Discord (with Developer Mode enabled in user settings), right-click each of the three priority channels → Copy ID. Edit `config/policy.yaml`, replacing the three `REPLACE_*_CHANNEL_ID` placeholders.

- [ ] **Step 2: Start the forwarder**

```
.venv/bin/python -m infra.bridge_client.discord_extension_forwarder
```

Expected log: `Discord extension forwarder listening on 127.0.0.1:9876`.

- [ ] **Step 3: Start the agent**

In a separate terminal:

```
.venv/bin/python main.py
```

Expected: `Bridge socket listening at /tmp/trading_bridge.sock` and the agent's normal startup logs.

- [ ] **Step 4: Open the three Discord channel tabs**

In Chrome with the extension loaded, open one tab per priority channel. Each tab's DevTools console should show `attached to messages list`.

- [ ] **Step 5: Trigger a real signal**

Either ask the user to post a known-safe test message in a priority channel, OR send via a Discord webhook into the same channel.

Within ~1 second, verify:
- Browser console: `forwarded <message_id> <preview>`
- Forwarder log: a `200`/`204` POST line
- Agent log: a `TriggerEvent` arriving with `source=discord_ext`, full body in `trigger_preview` (length > 250 chars confirms no truncation)

- [ ] **Step 6: Verify dedup with parallel notif-DB poller**

Leave the macOS notif DB bridge running. Post a second test message. Expected: agent's `IdempotencyStore` suppresses the slower DB-poller emit; only one downstream action runs.

- [ ] **Step 7: Record results**

If anything diverges from above, file a follow-up issue rather than amending the plan. The plan is complete when this end-to-end run succeeds.

- [ ] **Step 8: Commit any small fixes uncovered**

```
git add -p
git commit -m "fix(extension|forwarder): <issue uncovered during E2E>"
```

(Skip if E2E was clean.)

---

## Task 10: Operator setup documentation

**Files:**
- Create: `docs/ops/discord-extension-setup.md`

- [ ] **Step 1: Write the operator guide**

Create `docs/ops/discord-extension-setup.md`:

```markdown
# Discord Browser Extension — Setup

## What this is

A Chromium extension that captures full message text from three priority Discord
channels (`mystic`, `yonezu`, `stock-talk-portfolio`) and forwards it to the
trading agent. Replaces the truncated macOS notification capture for these
three channels. The macOS notif DB poller still runs and covers the other
watched channels at truncated quality.

## One-time setup

1. **Get the Discord channel IDs.** In Discord settings → Advanced, enable
   Developer Mode. Right-click each of the three priority channels → "Copy ID".
2. **Edit `config/policy.yaml`** — under `discord_extension.channel_id_map`,
   replace the three `REPLACE_*_CHANNEL_ID` strings with the real IDs.
3. **Load the extension.** Open `chrome://extensions`, enable Developer Mode
   (top-right), click "Load unpacked", select the `extension/` directory in
   this repo. Pin the extension's icon for easy reload during development.
4. **Open the three Discord channel tabs.** Pin them so they survive a Chrome
   restart.

## Per-session start

In order:

1. Start the forwarder:
   `.venv/bin/python -m infra.bridge_client.discord_extension_forwarder`
2. Start the macOS notif DB bridge as usual (covers the other channels).
3. Start the agent: `.venv/bin/python main.py`
4. Confirm the three Discord tabs are loaded; each tab's DevTools console
   should show `[trading-agent-ext] attached to messages list`.
5. Send a test message to one priority channel and confirm a
   `source=discord_ext` event reaches the agent log.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Console: "messages list never mounted" | Discord redesigned the DOM | Reopen `extension/test/harness.html` against fresh DOM and update `extract.js` selectors |
| Console: "Failed to forward signal after retries" | Forwarder not running | Start the forwarder (see above) |
| Forwarder log: "Dropping unmapped channel_id=..." | `channel_id_map` is wrong | Recopy the channel ID from Discord and update `config/policy.yaml` |
| Agent log: no `discord_ext` events | Tab was idle-disconnected by Discord | Reload the tab |

## Architecture reference

See `docs/superpowers/specs/2026-04-28-discord-browser-extension-capture-design.md`.
```

- [ ] **Step 2: Commit**

```
git add docs/ops/discord-extension-setup.md
git commit -m "docs(ops): add Discord browser extension setup guide"
```

---

## Done criteria

All of the following are true:

- `pytest tests/unit/test_discord_extension_policy.py tests/unit/test_discord_extension_forwarder.py tests/integration/test_discord_extension_forwarder.py -v` is green.
- `pytest tests/unit -q` and `pytest tests/integration -q` show no regressions.
- Loading `extension/test/harness.html` in a browser shows all `PASS`.
- The Task 9 end-to-end run produced a `discord_ext` `TriggerEvent` with full (>250-char) `trigger_preview` reaching the agent within ~1 second of the Discord post.
- `docs/ops/discord-extension-setup.md` exists and matches the actual setup steps that worked.
