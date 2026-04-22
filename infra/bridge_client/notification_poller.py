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

    def __init__(self, poll_interval: float = 0.3, initial_count: int | None = None) -> None:
        super().__init__(name="notification-banner-poller", daemon=True)
        self._interval = poll_interval
        self._last_count: int = initial_count if initial_count is not None else self._get_window_count()

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
        result = subprocess.run(
            ["osascript", "-e", _CLICK_SCRIPT],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            logger.warning("Click script failed (rc=%d): %s", result.returncode, result.stderr.strip())
