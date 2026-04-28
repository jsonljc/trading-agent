# Discord Browser Extension — Full-Text Signal Capture

**Status:** approved (2026-04-28)
**Replaces:** macOS notification DB capture for the 3 priority channels
**Keeps:** macOS notif DB capture (Spec [`2026-04-27-discord-notification-db-capture-design.md`](2026-04-27-discord-notification-db-capture-design.md)) for awareness of the other watched channels

## Why

The macOS notification database we built yesterday delivers detection but **not full message text**. Discord truncates banner bodies to ~250 characters. Live testing today against a real `Stock Talk Insiders` portfolio post showed the actual message at 1,173 characters — three quarters of the signal would have been silently lost.

The user posted two real example signals during validation: an entry post for `$SHEN` (~2,000 chars covering thesis, position size, capex math, FCF inflection year) and an `Everpure $P` deep-dive (~3,500 chars covering hardware differentiation, growth acceleration, peer comparison, position weighting). For these signal sources, the first 250 characters frequently don't even reach the position size or conviction level — only the headline ticker.

CDP-based attempts to extract from Discord's desktop client during today's session validated:
- Discord launches cleanly with `--remote-debugging-port=9222`
- The renderer is debuggable, JS evaluation works
- Webpack module finding (FluxDispatcher hook) is fragile and version-dependent
- Gateway WebSocket frames are zlib-stream binary, requiring a stateful decompression path

Direct DOM read of the rendered Discord web client showed:
- Every visible message is rendered as a stable DOM element (`[id^="chat-messages-<channelId>-<messageId>"]`)
- Full message body is in `[id^="message-content-<messageId>"]` as plain text — no truncation
- Author name available via `[class*="username"]`
- React virtualizes only off-screen messages; the visible viewport always contains complete text

A Chrome extension running a content script with a MutationObserver on the messages-list container is the cleanest, lowest-risk, lowest-fragility path to full-text capture for the priority channels.

## Goals

- Capture **complete message text** (no truncation) for the three priority channels: `mystic`, `yonezu`, `stock-talk-portfolio`.
- **Sub-second** end-to-end latency from message arrival in Discord to event on the bridge socket.
- **Zero ToS risk** — purely passive observation of DOM that Discord renders for the user's normal use.
- Keep the existing socket protocol unchanged; the agent and listener don't need code changes beyond a small dedup tweak.

## Non-goals

- Capturing all 13 watched channels in full text. The macOS notif DB poller continues to provide truncated capture there.
- Modifying Discord's JS, hooking webpack modules, or running BetterDiscord-style plugins. (Lower fragility, lower ToS risk.)
- Capturing DMs, threads, message edits, or replies. (Out of scope; ignore them.)
- Cross-server support. The three priority channels live in the same `Stock Talk Insiders` server.

## Architecture

```
┌── Chrome / Brave / Arc (any Chromium) ──────────────────────────┐
│                                                                 │
│  Tab 1: https://discord.com/channels/<server>/<mystic-id>       │
│  Tab 2: https://discord.com/channels/<server>/<yonezu-id>       │
│  Tab 3: https://discord.com/channels/<server>/<stock-talk-id>   │
│                                                                 │
│  Each tab loads the extension's content script automatically:   │
│    1. Wait for the messages-list container to render            │
│    2. Snapshot existing visible messages once (initial state)   │
│    3. Install MutationObserver on the messages-list container   │
│    4. On any added node matching `[id^="chat-messages-"]`:      │
│         - extract author, content, message_id, timestamp        │
│         - POST JSON to http://localhost:9876/signal             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌── DiscordExtensionForwarder (Python, new) ─────────────────────┐
│                                                                │
│  HTTP server on 127.0.0.1:9876                                 │
│   POST /signal                                                 │
│     body: { channel, author, content, message_id, timestamp }  │
│                                                                │
│  Normalizes channel name to canonical (lowercased, no emoji),  │
│  writes one newline-delimited JSON event to                    │
│  /tmp/trading_bridge.sock matching the bridge's existing       │
│  envelope:                                                     │
│   { event_id, source: "discord_ext", channel, author,          │
│     trigger_preview, received_at }                             │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌── Existing Python agent pipeline (unchanged) ──────────────────┐
│                                                                │
│  /tmp/trading_bridge.sock listener in main.py reads the event  │
│  shape it already understands. SignalAnalyzer / TickerValidator│
│  / etc. run unchanged.                                         │
│                                                                │
└────────────────────────────────────────────────────────────────┘

  ┌── macOS notif DB bridge (existing, unchanged) ──────────────┐
  │  Continues capturing all 13 watched channels at truncated   │
  │  quality for awareness. Dedup against discord_ext events    │
  │  prevents double-firing on the priority three.              │
  └─────────────────────────────────────────────────────────────┘
```

## Components

### Chrome extension (new) — Manifest V3

**Files:**
- `extension/manifest.json`
- `extension/content.js`
- `extension/background.js` (only if needed for cross-origin POST relaxation)
- `extension/icons/icon-128.png` (placeholder)

**Manifest highlights:**
- `manifest_version: 3`
- `host_permissions: ["https://discord.com/*"]`
- `permissions: []` — no extra permissions needed
- `content_scripts:` matches `https://discord.com/channels/*`, runs at `document_idle`

**Content script behavior:**
1. On load, wait (poll-and-retry up to 30s) for the messages container to mount. Discord lazily loads it after channel switch.
2. Read the URL to extract `(server_id, channel_id)`. The content script does not know the human channel name yet — it sends `channel_id` and lets the forwarder map it.
3. Initial snapshot: read all currently-visible message rows. **Don't emit them** (these are historical). Just record their IDs into a seen-set so the MutationObserver skips them on its first burst.
4. Install MutationObserver on the messages container, watching for childList additions.
5. For each added node matching `[id^="chat-messages-"]` not already in the seen-set:
   - Extract `message_id` from the element id
   - Extract `content` from `[id^="message-content-"]` (innerText)
   - Extract `author` from `[class*="username"]` (innerText)
   - Extract `timestamp` from the `<time>` element's `datetime` attribute
   - POST `{ channel_id, server_id, author, content, message_id, timestamp }` to `http://localhost:9876/signal`
6. On idle-tab disconnect (Discord shows "You've been disconnected" overlay), reload the tab. (Discord drops idle tabs after ~10-15 min of zero interaction.)

**Selector resilience:** All selectors are attribute-based (`[id^=...]`, `[class*=...]`) rather than class-name-based, since Discord rotates class hashes on every release.

### DiscordExtensionForwarder (new Python service)

**File:** `infra/bridge_client/discord_extension_forwarder.py`

**Behavior:**
- Starts an `aiohttp` (or stdlib `http.server`) HTTP server bound to `127.0.0.1:9876`.
- Accepts `POST /signal` with the JSON body shape above.
- Looks up `channel_id` in a config-driven map → canonical channel name (`mystic` / `yonezu` / `stock-talk-portfolio`). Drops events from unmapped channels.
- Connects to `/tmp/trading_bridge.sock` as a client (the bridge already serves it). Writes one newline-delimited JSON event per signal.
- Reconnects on socket close.
- Logs every received signal at `INFO` to `logs/discord_extension.log`.

**Config:** A new section in `config/policy.yaml`:
```yaml
discord_extension:
  channel_id_map:
    "<discord-channel-id-for-mystic>": "mystic"
    "<discord-channel-id-for-yonezu>": "yonezu"
    "<discord-channel-id-for-stock-talk-portfolio>": "stock-talk-portfolio"
  forwarder_port: 9876
```

The user fills in real channel IDs once at install time (one-shot from a Discord URL: the long number after `/channels/<server>/<channel>`).

### Bridge dedup tweak

**File:** `bridge/Sources/NotificationBridge/FingerprintDedup.swift` (and a new field on the socket envelope).

When a `discord_ext` event arrives with a `message_id`, the dedup uses `message_id` as the fingerprint key. When a `notif_db` event arrives, it uses the existing `(channel, author, body)` fingerprint. If both fire for the same Discord message:
- Extension fires faster (sub-second vs the DB poller's 500 ms loop)
- Extension's full-text fingerprint won't match the truncated DB fingerprint, so dedup wouldn't suppress
- Add a secondary dedup pass keyed on `(channel, author, content_prefix_64chars)` so the slower DB-poller emit is suppressed when the faster extension emit already won

**Simpler alternative:** the agent itself already has idempotency at the signal level. Let both fire. Whichever arrives first establishes the trace; the second hits the existing `IdempotencyStore` and gets dropped at the agent. No bridge-level change needed.

We adopt the simpler alternative. **No bridge change required.**

## Data flow

1. User posts `OPEN $SHEN ...` in `#stock-talk-portfolio`.
2. Discord renderer in Tab 3 receives the gateway frame, decompresses, dispatches to its React store, renders a new `<li id="chat-messages-...">` into the DOM.
3. MutationObserver in the content script fires within ~10 ms of the DOM mutation.
4. Content script extracts `(message_id, channel_id, author=stock-talk-weekly, content=full ~2000 chars, timestamp)`.
5. Content script POSTs to `http://localhost:9876/signal`.
6. Forwarder maps `channel_id` → `stock-talk-portfolio`, builds the bridge envelope, writes to `/tmp/trading_bridge.sock`.
7. Agent's `SocketReader` ingests as a normal trigger event with full body.

End-to-end latency: ~50-200 ms.

## Error handling

- **Forwarder unreachable** (Python not running, port closed): content script catches the fetch error, retries up to 3× with 500 ms backoff, then drops the message and logs to the extension's console. The notif DB poller still catches a truncated version as backup, so we don't lose the signal entirely.
- **Forwarder running, bridge socket missing**: forwarder buffers up to 100 recent signals in memory and reconnects when the socket appears.
- **Tab disconnected by Discord**: content script reloads the tab. State is rebuilt from the initial snapshot.
- **Discord redesign breaks selectors**: extension logs a console warning if the messages container can't be found within 30 s. User notices when capture stops; we update selectors. The notif DB poller continues providing truncated coverage during the outage.
- **Channel ID changes**: the channel ID map in `config/policy.yaml` becomes wrong. Extension still POSTs; forwarder drops with a log warning. User updates the map.

## Testing

- **Unit:** Forwarder's channel-id mapping function (Python). Pytest, ~5 cases.
- **Unit:** Content script's selector extraction (Jest with jsdom OR a tiny harness page that loads a saved Discord HTML snapshot). ~5 cases covering the message shapes seen in real signal posts (text only, with mentions, with embeds, with attachments).
- **Integration:** End-to-end on a real local Chrome with the extension installed and Discord open. Send a synthetic message via Discord webhook to a test channel; assert the agent receives it. Manual gate, ~3 minutes.

## Setup and operations

**One-time setup:**
1. Build the extension (`cd extension && zip -r ../extension.zip .` or just leave unpacked).
2. In Chrome: `chrome://extensions` → enable Developer Mode → "Load unpacked" → select the `extension/` directory.
3. Pin the extension icon for easy reload during development.
4. Get the channel IDs from Discord (right-click channel → Copy ID, requires Developer Mode in Discord client) and add to `config/policy.yaml`.
5. Open three Chrome tabs, one per priority channel. Pin them.

**Per-session start:**
1. Ensure the three Discord tabs are loaded.
2. Start the Python forwarder: `.venv/bin/python -m infra.bridge_client.discord_extension_forwarder`.
3. Start the macOS notif DB bridge as today (still useful for the other 10 channels).
4. Start `main.py` agent.
5. Send a test message to one priority channel; confirm it arrives at the agent.

## Migration

This adds a new path. The existing macOS notif DB bridge stays running unchanged. No DB schema changes. No agent code changes. Just two new components and one config block.

## Out of scope (filed for later)

- Auto-reconnect on Discord WebSocket drop without full tab reload.
- Capturing message edits, deletes, or pinned changes.
- Capturing image OCR (the extension only reads text).
- Multi-server support — current scope is the one Stock Talk Insiders server.
- Publishing the extension to the Chrome Web Store. (Loaded unpacked is fine for personal use.)
