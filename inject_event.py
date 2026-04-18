#!/usr/bin/env python3
"""
Dev tool: inject a fake trigger event into the bridge socket for testing.

Usage:
    python inject_event.py "Long $AVEX today's IPO" --channel mystic --author UndefinedMystic
    python inject_event.py "Watching $TSLA" --channel alerts
"""
import argparse
import json
import socket
import uuid
from datetime import datetime, timezone

parser = argparse.ArgumentParser()
parser.add_argument("preview", help="Message preview text")
parser.add_argument("--channel", default="mystic")
parser.add_argument("--author", default="UndefinedMystic")
parser.add_argument("--socket", default="/tmp/trading_bridge.sock")
args = parser.parse_args()

event = {
    "event_id": str(uuid.uuid4()),
    "source": "injected",
    "channel": args.channel,
    "author": args.author,
    "trigger_preview": args.preview,
    "received_at": datetime.now(timezone.utc).isoformat(),
}

payload = json.dumps(event).encode() + b"\n"

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.connect(args.socket)
    s.sendall(payload)
    print(f"Injected: {event['event_id']} | {args.channel} | {args.preview[:60]}")
