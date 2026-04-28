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
