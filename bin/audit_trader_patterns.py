#!/usr/bin/env python3
"""
Audit each trader profile's discord_author_pattern against the authors
actually captured in signal_events. Prints a table; exits non-zero if
any tracked channel has < 50% of its signals matched by the configured
pattern (drift detector).

Usage:
    python bin/audit_trader_patterns.py
    python bin/audit_trader_patterns.py --since 2026-04-15
    python bin/audit_trader_patterns.py --db data/trading_agent.db
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import yaml


def load_profiles(directory: Path) -> dict[str, dict]:
    """handle -> {pattern, channel_hint}. channel_hint is the YAML basename."""
    out: dict[str, dict] = {}
    for p in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text())
        out[raw["handle"]] = {
            "pattern": raw["discord_author_pattern"],
            "channel_slug_hint": p.stem,
        }
    return out


def channels_for_handle(channel_id_map: dict[str, str], handle: str) -> set[str]:
    return {slug for _id, slug in channel_id_map.items() if slug == handle}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--since", default=None,
                        help="ISO date, e.g. 2026-04-15. Default: all time.")
    parser.add_argument("--policy", default="config/policy.yaml")
    parser.add_argument("--traders", default="config/traders")
    args = parser.parse_args()

    policy = yaml.safe_load(Path(args.policy).read_text())
    channel_id_map = policy.get("discord_extension", {}).get("channel_id_map", {})
    profiles = load_profiles(Path(args.traders))

    where = "WHERE 1=1"
    params: list[str] = []
    if args.since:
        where += " AND received_at >= ?"
        params.append(args.since)

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        f"SELECT channel, author, COUNT(*) FROM signal_events {where} "
        "GROUP BY channel, author ORDER BY channel, COUNT(*) DESC",
        params,
    ).fetchall()
    conn.close()

    by_channel: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for ch, au, n in rows:
        by_channel[ch].append((au or "", n))

    fail = False
    print(f"{'Trader':<18} {'Pattern':<28} {'Channel':<22} {'Match':>6} {'Misses':>50}")
    print("-" * 130)
    for handle, info in profiles.items():
        pattern = info["pattern"]
        channels = channels_for_handle(channel_id_map, handle)
        if not channels:
            channels = {info["channel_slug_hint"]}
        for ch in sorted(channels):
            counts = by_channel.get(ch, [])
            total = sum(n for _, n in counts)
            matched = sum(n for au, n in counts if au == pattern)

            # Drift heuristic: ignore empty-author rows (separate capture
            # bug). Compare configured pattern against the most frequent
            # NON-EMPTY author. Drift iff pattern is not the modal non-empty
            # author AND barely matches anything (< 5 messages). Multi-poster
            # channels (e.g. Naz also posts in pup-danny's channel) stay OK
            # as long as the configured trader is the dominant voice.
            non_empty = [(au, n) for au, n in counts if au]
            non_empty.sort(key=lambda x: -x[1])
            modal_non_empty = non_empty[0][0] if non_empty else None
            empty_count = sum(n for au, n in counts if not au)

            # Empties already reported separately as `empty=N`; exclude from misses.
            non_match = [(au, n) for au, n in counts if au and au != pattern]
            non_match.sort(key=lambda x: -x[1])
            misses = ", ".join(f"{au!r}={n}" for au, n in non_match[:5]) or "-"
            if empty_count:
                misses = f"empty={empty_count}; {misses}"

            pct = (matched / total * 100) if total else 0.0
            if total == 0:
                tag = "no-data"
            elif modal_non_empty != pattern and matched < 5:
                tag = "DRIFT"
                fail = True
            else:
                tag = "OK"
            print(f"{handle:<18} {pattern!r:<28} {ch:<22} "
                  f"{matched:>3}/{total:<3} ({pct:5.1f}%) [{tag}]  {misses}")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
