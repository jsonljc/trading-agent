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
