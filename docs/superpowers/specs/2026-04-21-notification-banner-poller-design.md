# Notification Banner Poller — Design Spec
Date: 2026-04-21

## Problem

The Swift bridge uses a CGEvent-based banner clicker that silently fails because background CLI processes lack GUI session access on macOS 26. The AX title watcher in the bridge works correctly once Discord navigates to a channel — the missing piece is automating the click.

## Solution

A Python background thread (`NotificationBannerPoller`) that polls macOS `NotificationCenter` for new banner windows and clicks them via `osascript`, which runs in the user's GUI session.

## Architecture

```
Banner appears (top-right)
  → NotificationBannerPoller detects new NotificationCenter window
  → osascript clicks window center
  → Discord navigates to channel
  → AX title watcher (Swift bridge) detects window title change
  → Reconcile captures message text
  → Event emitted to Python agent via Unix socket
  → Pipeline runs (desktop_reader → trade_intent_detector → ...)
```

## Components

### `infra/bridge_client/notification_poller.py`
- `NotificationBannerPoller(threading.Thread)` — daemon thread, started once at agent startup
- Polls every 0.3s using `osascript` to count `NotificationCenter` windows
- On new window: second `osascript` call gets position/size, clicks center via `System Events click at {x, y}`
- Clicks **any** new Discord banner (can't read banner text on macOS 26; title watcher filters by channel)
- All exceptions caught and logged — never crashes the agent

### `main.py` change
- Import and start `NotificationBannerPoller` as a daemon thread after agent setup, before `reader.start()`

## No Swift bridge changes needed
The existing title watcher already handles navigation detection and reconcile triggering.

## Success Criteria
- A Discord notification banner fires → bridge logs `Window title changed` to a watched channel → agent receives and processes the event — all without user interaction
