"""HTTP-to-bridge-socket forwarder for the Discord browser extension.

Receives POSTs from a Chromium content script with full Discord message text
and forwards them to the agent's existing Unix-socket trigger pipeline.
"""
from __future__ import annotations

import json
import logging
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
