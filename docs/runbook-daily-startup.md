# Daily Trading Session Startup Checklist

The bridge captures Discord signals through the macOS notification database. macOS has a half-dozen things that can silently kill capture — this checklist walks them off one by one.

## Before the session

### 1. Mac state
- **Plugged in.** Charger connected. Battery-only causes deeper sleep policies.
- **Caffeinate active.** `pmset -g | grep "sleep"` should show `sleep` is `0` or "prevented by caffeinate". If not, run `caffeinate -d -i &` in a background terminal.
- **Screen can lock**, but **screen saver should not run a process that grabs focus.**
- **Discord open**, logged into the relevant server.

### 2. macOS Focus mode → OFF
Active Focus silences notifications, which means **zero rows hit the notification DB and the bridge captures nothing**. This is the most common silent-failure mode.

- Menu bar → Control Center → Focus tile → click any active mode to turn off.
- Verify via `bin/agent-status` (it prints a warning if any focus mode is active).
- **Also disable Smart Trigger schedules** (Focus → Settings → schedule rules) for the session window. Personal Time / Sleep modes auto-engage on a schedule and will silently kick in mid-session if not disabled.

### 3. Discord channel notifications
For every channel in the watched list (`mystic, alerts, trades, wall-st-engine, stock-talk-portfolio, chat, yonezu, pup-danny, urkel, gladiator, graddox, phat, grid`):

- Right-click the channel → **Notification Settings** → **All Messages** (not "Only @Mentions" or "Nothing").
- Server-level mute also off.
- Discord app's own DND (`User Settings → Notifications → Enable Push Notifications`) must be ON.

A muted channel writes nothing to the DB → nothing to capture.

### 4. IB Gateway
- Launched, logged in (paper account `DU…`).
- API enabled, port `4002`.
- No other IB session active anywhere (mobile app, TWS desktop, second Gateway). "Competing live session" error means a duplicate login is stealing market data.

### 5. Full Disk Access
- The bridge binary at `bridge/.build/debug/NotificationBridge` must be in **System Settings → Privacy & Security → Full Disk Access** with the toggle ON.
- After every `swift build`, **re-sign the binary** so its cdhash stays stable: `codesign -s - --force bridge/.build/debug/NotificationBridge`. (Without this, FDA grant invalidates and silently denies DB reads.)
- The terminal you start the bridge from must also have FDA (or the bridge inherits a denied scope from the parent shell).

## Starting the session

```bash
# From a terminal that has Full Disk Access
cd /Users/jasonli/dev/trading-agent
rm -f /tmp/trading_bridge.sock
.venv/bin/python bin/agent-listen-only &        # passive listener for verification
bridge/.build/debug/NotificationBridge /tmp/trading_bridge.sock &
```

Verify both came up:
```bash
ps aux | grep -E "NotificationBridge|agent-listen" | grep -v grep
```

The bridge's first log line should read:
```
NotificationDBPoller running. db=... startingRecId=<NUMBER>
```
A non-zero `startingRecId` means the DB is readable — the FDA chain works.

## During the session

- Watch `bin/agent-status` periodically — it'll flag Focus mode if it sneaks back on.
- Keep the bridge terminal window open. Closing the terminal kills the bridge.
- Mac can be locked. Notifications still flow to the DB while locked.

## Stopping the session

```bash
pkill -f "NotificationBridge /tmp/trading_bridge"
pkill -f agent-listen-only
rm -f /tmp/trading_bridge.sock
```

## Common silent failures

| Symptom | Likely cause |
|---|---|
| `startingRecId=0` at startup | FDA not granted (re-sign binary, re-grant in System Settings) |
| Bridge logs `reconcile: …` continuously but listener empty | You're focused on a watched channel (banners suppressed); or Focus mode is on; or channels muted |
| `MAX(rec_id)` in DB advances but no events emitted | Decoder bug — re-check `DiscordTitleParser` against actual notification format |
| All AX events emit, no `notif_db` events | Bridge DB poll is failing. Check stderr for `sqlite3_open_v2 failed (rc=23)` — TCC denial |

## Operational caveats

- Notification body is truncated at ~250 chars by Discord. Long signal posts may lose context.
- Image-only posts give body `"Uploaded image.png"` — no signal extractable; rely on AX fallback.
- Discord message edits don't generate new notifications. Original capture only.
- macOS auto-updates can reboot overnight. Defer updates during active trading weeks.
