# Notification Banner Poller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Python daemon thread that polls macOS NotificationCenter for new Discord notification banners and clicks them via osascript, so the AX title watcher can automatically capture messages without user interaction.

**Architecture:** `NotificationBannerPoller` runs as a daemon thread inside the existing Python agent. It polls the `NotificationCenter` process every 0.3s using `osascript`, detects new windows (banner appeared), reads their position/size, and clicks the center via `System Events`. The existing Swift AX title watcher then detects Discord's navigation and triggers message capture.

**Tech Stack:** Python `threading`, `subprocess`, `osascript` (AppleScript), existing `main.py` + `infra/bridge_client/` structure.

---

### Task 1: Create `NotificationBannerPoller`

**Files:**
- Create: `infra/bridge_client/notification_poller.py`
- Test: `tests/unit/test_notification_poller.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_notification_poller.py
import threading
from unittest.mock import patch, MagicMock
from infra.bridge_client.notification_poller import NotificationBannerPoller

def test_is_daemon_thread():
    poller = NotificationBannerPoller()
    assert poller.daemon is True

def test_does_not_click_when_count_unchanged():
    poller = NotificationBannerPoller()
    with patch("infra.bridge_client.notification_poller.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="2\n", returncode=0)
        poller._last_count = 2
        poller._poll()
        # Only one call to get count, no click call
        assert mock_run.call_count == 1

def test_clicks_when_count_increases():
    poller = NotificationBannerPoller()
    with patch("infra.bridge_client.notification_poller.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="3\n", returncode=0)
        poller._last_count = 2
        poller._poll()
        # Two calls: count check + click
        assert mock_run.call_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/jasonli/dev/trading-agent
.venv/bin/pytest tests/unit/test_notification_poller.py -v
```
Expected: `ModuleNotFoundError: No module named 'infra.bridge_client.notification_poller'`

- [ ] **Step 3: Implement `notification_poller.py`**

```python
# infra/bridge_client/notification_poller.py
import logging
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

_COUNT_SCRIPT = """
tell application "System Events"
    tell process "NotificationCenter"
        count windows
    end tell
end tell
"""

_CLICK_SCRIPT = """
tell application "System Events"
    tell process "NotificationCenter"
        set wins to windows
        if (count wins) > 0 then
            set w to item 1 of wins
            set pos to position of w
            set sz to size of w
            set cx to (item 1 of pos) + (item 1 of sz) / 2
            set cy to (item 2 of pos) + (item 2 of sz) / 2
            click at {cx, cy}
        end if
    end tell
end tell
"""

class NotificationBannerPoller(threading.Thread):
    """Polls macOS NotificationCenter for new Discord banners and clicks them.

    Clicking navigates Discord to the relevant channel; the Swift AX title
    watcher then detects the navigation and captures the message.
    """

    def __init__(self, poll_interval: float = 0.3) -> None:
        super().__init__(name="notification-banner-poller", daemon=True)
        self._interval = poll_interval
        self._last_count: int = self._get_window_count()

    def run(self) -> None:
        while True:
            try:
                self._poll()
            except Exception:
                logger.exception("NotificationBannerPoller error")
            time.sleep(self._interval)

    def _poll(self) -> None:
        count = self._get_window_count()
        if count <= self._last_count:
            self._last_count = count
            return
        self._last_count = count
        logger.info("New notification banner detected (count=%d) — clicking", count)
        self._click_banner()

    def _get_window_count(self) -> int:
        result = subprocess.run(
            ["osascript", "-e", _COUNT_SCRIPT],
            capture_output=True, text=True, timeout=2,
        )
        try:
            return int(result.stdout.strip())
        except ValueError:
            return 0

    def _click_banner(self) -> None:
        subprocess.run(
            ["osascript", "-e", _CLICK_SCRIPT],
            capture_output=True, text=True, timeout=3,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/unit/test_notification_poller.py -v
```
Expected: all 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add infra/bridge_client/notification_poller.py tests/unit/test_notification_poller.py
git commit -m "feat: add NotificationBannerPoller daemon thread

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Wire poller into `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add import and start the poller in `run()`**

In `main.py`, add the import at the top with the other bridge imports:

```python
from infra.bridge_client.notification_poller import NotificationBannerPoller
```

Then in the `run()` async function, just before `await reader.start(handle_event)`, add:

```python
    NotificationBannerPoller().start()
    logger.info("Notification banner poller started")
```

The full bottom of `run()` should look like:

```python
    reader = SocketReader(socket_path)
    logger.info("Trading agent Phase 1 ready. Listening on %s", socket_path)
    try:
        NotificationBannerPoller().start()
        logger.info("Notification banner poller started")
        await reader.start(handle_event)
    finally:
        await conn.close()
```

- [ ] **Step 2: Restart the agent and verify poller starts**

```bash
pkill -f "main.py" 2>/dev/null; sleep 1
ANTHROPIC_API_KEY=<your-api-key> .venv/bin/python main.py > /tmp/trading_agent.log 2>&1 &
sleep 2 && cat /tmp/trading_agent.log
```

Expected log output includes:
```
INFO main: Notification banner poller started
INFO main: Trading agent Phase 1 ready. Listening on /tmp/trading_bridge.sock
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: start NotificationBannerPoller in agent main loop

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Integration verification

**Files:** no new files — verify end-to-end

- [ ] **Step 1: Ensure bridge is running**

```bash
pgrep -a NotificationBridge || (cd /Users/jasonli/dev/trading-agent && bridge/.build/release/NotificationBridge /tmp/trading_bridge.sock data/ax_events.log > /tmp/bridge.log 2>&1 &)
```

- [ ] **Step 2: Wait for a `#chat` notification banner to appear, then verify auto-capture**

Watch the logs:
```bash
tail -f /tmp/trading_agent.log
```

Expected sequence when a Discord `#chat` banner fires:
```
INFO notification_poller: New notification banner detected (count=N) — clicking
# bridge.log:
Window title changed: '#🏦丨chat | Stock Talk Insiders - Discord' -> channel='chat'
reconcile: activeChannel='chat' ...
# agent log:
INFO main: Received event <uuid> from #chat by <author>
```

- [ ] **Step 3: If poller clicks on startup (false positive), add warmup sleep**

If banner clicks happen immediately on start, the baseline count was wrong. Add a 1-second warmup in `run()`:

```python
def run(self) -> None:
    time.sleep(1.0)  # let startup settle
    self._last_count = self._get_window_count()  # re-baseline after warmup
    while True:
        try:
            self._poll()
        except Exception:
            logger.exception("NotificationBannerPoller error")
        time.sleep(self._interval)
```

Then re-run Step 2 and re-commit:

```bash
git add infra/bridge_client/notification_poller.py
git commit -m "fix: re-baseline banner count after warmup sleep

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
