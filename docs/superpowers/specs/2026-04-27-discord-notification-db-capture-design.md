# Discord Signal Capture via macOS Notification Database

**Status:** approved (2026-04-27)
**Replaces:** parts of the AX-based bridge (`AXDiscordWatcher`, `NotificationBannerClicker`, Python `NotificationBannerPoller`)
**Keeps:** `AXDiscordWatcher` as a fallback for the active-channel case

## Why

Today's bridge captures Discord signals by walking macOS's Accessibility (AX) tree and clicking notification banners on screen. That approach is fragile in three ways:

1. **The banner clicker often fails to fire.** Observed in this session: 0 clicks in 430 lines of bridge output despite multiple banners arriving on screen. The Swift `NotificationBannerClicker` polls `NotificationCenter` for new windows; on macOS 26 those windows may not appear where it looks.
2. **AX events fire on every UI change**, not just new messages. When the user navigates between channels, the bridge emits events whose `body` is the channel header (e.g. `#🏦丨chat | Stock Talk Insiders - Discord`) instead of message content. These force a downstream LLM call that classifies them as "not a trade signal" and discards them — wasted tokens.
3. **AX text extraction is brittle.** When Discord ships a UI redesign, the AX walker has to be re-tuned. The current extractor sometimes returns window titles and channel-name text instead of message bodies.

A better source of truth already exists: **macOS's notification database.** Every Discord notification is recorded in `~/Library/Application Support/com.apple.notificationcenter/db2/db` (SQLite) with structured `title` / `subtitle` / `body` fields. Reading from there bypasses the UI entirely.

The repository already contains `bridge/Sources/NotificationBridge/NotificationPoller.swift` that reads this database — but **it is dead code** (instantiated nowhere in `main.swift`). This spec wires it up as the primary signal source.

## Goals

- Capture every Discord notification in a watched channel reliably, regardless of which Discord channel/tab is focused, regardless of whether the macOS banner is visible long enough to click.
- Eliminate noise events (channel headers, navigation triggers) that drive wasted LLM calls.
- Reduce end-to-end detection latency from ~2.3 s to ≤ 0.6 s.
- Keep the existing socket protocol so `main.py` and `inject_event.py` continue to work unchanged.

## Non-goals

- Replacing AX entirely. AX still has one job: capturing messages that arrive in a channel the user is *currently focused on* (Discord doesn't generate macOS notifications for the active channel).
- Capturing edits, deletes, image-only posts, or messages from muted channels. macOS notifications don't carry that information; today's AX bridge doesn't catch them either.
- Building any kind of bot, selfbot, or Discord API integration. Out of scope per user constraint (paid signal services where bots are not allowed and ToS risk on the user account is unacceptable).

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Discord posts a message in a watched channel       │
└─────────────────────────────────────────────────────┘
              │                            │
              │                            │
              ▼                            ▼
   macOS notif DB row             Discord renders msg
   (always, unless user is        in the open channel
   focused on this channel)       view (only if user
                                  is viewing it)
              │                            │
              ▼                            ▼
   NotificationDBPoller            AXDiscordWatcher
   (Swift, 500 ms tick,            (existing, unchanged;
   reads SQLite DB,                fires only on AX
   parses title/sub/body)          value-changed events)
              │                            │
              └─────────┬──────────────────┘
                        ▼
              Fingerprint dedup
              (existing ring buffer in
              AXDiscordWatcher; reuse)
                        │
                        ▼
              SocketEmitter (existing)
                        │
                        ▼
              /tmp/trading_bridge.sock → agent
```

## Components

### NotificationDBPoller (new, replaces the dead `NotificationPoller.swift`)

Polls the macOS notification database. For each new record where `appBundleId == "com.hnc.Discord"`:

- **Parse** `title` (server name), `subtitle` (channel name like `#mystic` — strip leading `#` and any emoji prefix), `body` (`"author: message text"` — split on first `: `).
- **Filter** to channels in the watched-channels list.
- **Emit** to the existing dedup + socket pipeline as `source="notif_db"`.

Polling interval: **500 ms**. The DB read is a small indexed query (`WHERE rec_id > ?`) and costs ~5 ms; 500 ms gives sub-second detection while not hammering the disk.

The existing `NotificationPoller.swift` already opens the DB in read-only mode and decodes records via `NSKeyedUnarchiver`. We extend it to:
- Maintain `lastSeenId` across polls (already does).
- On startup, initialize `lastSeenId` to the **current max `rec_id`** to avoid replaying historical notifications on first boot.
- Schedule itself via `Timer.scheduledTimer` from `main.swift`.

### Channel name parser (new helper)

Discord's `subtitle` field for a notification looks like `#🏦丨chat`. We need to map this to the watched-channel string `chat`. Helper:

- Strip leading `#`.
- Strip leading emoji and the `丨` separator if present (some servers prefix channel names with emoji + box-drawing character).
- Lowercase, hyphen-normalize.
- Compare to `watchedChannels` set.

If a channel doesn't match, drop the notification silently.

### Author parser

Discord notification bodies start with `"<author>: <message>"` for most server posts. Helper:

- Split on first `": "`.
- Left side → author. Right side → message body.
- If no `": "` separator (DMs, system messages), use empty author and full body — same shape as today's AX path.

### `AXDiscordWatcher` — keep but downscope

Keep firing as today, but its role is reduced to "catch messages while user is viewing the channel". Two changes:

- **Tighten extraction filter** (small fix, addresses the noise issue). Drop messages that:
  - Match the window title pattern (`* | Stock Talk Insiders - Discord`).
  - Are channel headers (start with `#` followed by emoji + channel name).
  - Have empty author AND body length < 30 chars (low-confidence noise).
- **Source label** stays `ax_event` so we can tell where the signal came from in audit logs.

### `NotificationBannerClicker` — delete

No longer needed. The whole point of clicking the banner was to get Discord to navigate to the channel so the AX watcher could read it. With the DB poller covering all non-active channels, navigation isn't needed.

Delete `NotificationBannerClicker.swift` and remove its instantiation from `main.swift`.

### Python `NotificationBannerPoller` — delete

Lives in `infra/bridge_client/notification_poller.py`. Same reasoning — no longer needed once the DB poller is the primary path. Remove the import and `start()` call from `main.py`.

### `SocketEmitter` — unchanged

Existing newline-delimited JSON over Unix domain socket. Both poller paths feed it. The Python agent (`SocketReader` in `infra/bridge_client/socket_reader.py`) needs no changes — same payload schema.

### Dedup — reuse existing fingerprint logic

`AXDiscordWatcher` already has a ring-buffer fingerprint dedup (`seenFingerprints`, `fingerprint(channel:author:body:)`, `markSeen(_)`). Move this into a shared `FingerprintDedup` actor/class so both pollers can use it without sharing other state.

## Data flow / payload

The JSON payload over the socket stays as it is today. New `source` value `notif_db` joins the existing `ax_event` and `reconciliation` values; the agent's pipeline doesn't branch on `source`.

```json
{
  "event_id": "<uuid>",
  "source": "notif_db",
  "channel": "mystic",
  "author": "UndefinedMystic",
  "trigger_preview": "Initiating long position in $SPY...",
  "received_at": "2026-04-27T22:30:14.123Z"
}
```

## Permissions / setup

- **Full Disk Access** must be granted to the bridge binary in System Settings → Privacy & Security → Full Disk Access. The notification DB is TCC-protected.
- **Watched channels must not be muted** in Discord (muting suppresses macOS notifications, which means no DB row, which means no signal). Document this in the runbook.
- **Discord notification settings** for each watched channel should be set to "All Messages" (not "Only @mentions").

## Error handling

- **DB unreadable** (TCC denied, file locked, file moved): log a warning, return empty result. Poller keeps trying. AX path still works as fallback.
- **`NSKeyedUnarchiver` decode fails** for a record: skip that record, advance `lastSeenId`, log debug. Don't crash the poll loop.
- **Channel name fails to parse**: skip silently. We'd rather miss a signal than emit garbage.
- **Author parse fails**: emit with empty author. SignalAnalyzer downstream tolerates this.

## Testing

- Unit test for the channel name parser: `#🏦丨chat` → `chat`, `#mystic` → `mystic`, `#alerts` → `alerts`, plain `chat` → `chat`.
- Unit test for the author parser: `"User: msg"` → `("User", "msg")`; `"msg with no colon"` → `("", "msg with no colon")`.
- Integration test: drop a synthetic row into a temp SQLite DB matching the schema, verify `pollNew()` returns it.
- Manual end-to-end: run the bridge, send a real Discord message in a watched channel, watch the listener output. (No automated test for this.)

## Migration

This is a one-shot change to `bridge/`. No DB schema migration. No protocol change. The Python agent doesn't need to be redeployed before the bridge is updated; old AX events keep working until the bridge is restarted.

## Out of scope (filed for later)

- Capturing image-only posts via OCR on the screenshot fallback. Spec 2 already established AX as primary; this design does not regress that.
- Recovering muted-channel signals via direct DOM scraping or selfbot. User declined ToS risk.
- Daily-rotation of the notification DB if it grows too large. The macOS system rotates it; we just read.
