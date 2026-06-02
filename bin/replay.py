#!/usr/bin/env python3
"""Replay / backtest harness — replay captured alerts through the LIVE skill
chain in paper mode with deterministic fakes and NO real orders.

Opens the live DB READ-ONLY (never writes), loads signal_events (filtered/limited)
and the recorded LLM responses from classification_log, replays each alert through
the real phase1 + phase2b chains, and reports the classify -> gate -> size ->
execute decision for each (with the would-be order it WOULD have placed). Also
flags DIVERGENCE: replayed bucket/action vs the recorded classification_log
bucket/action — a replay-correctness check.

Read-only / paper / deterministic by construction:
- live DB is opened mode=ro; all chain writes go to a fresh in-memory DB per alert
- ReplayGateway never networks and never places a real order
- RecordedClassifierClient replays the recorded LLM decision (zero API calls)
- execution eligibility is pinned to a fixed RTH clock

Usage:
    python bin/replay.py
    python bin/replay.py --channel stocktalkweekly --limit 20
    python bin/replay.py --event-id discord_ex_abc123
    python bin/replay.py --net-liq 250000 --quote 100 --json
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

# Allow running as `python bin/replay.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite

from agent.policy import load_policy
from agent.replay.recorded_llm import RecordedClassifierClient
from agent.replay.runner import replay_all


async def _load(db_path, *, channel=None, limit=None, event_id=None):
    """Load signal_events (filtered) and the recorded LLM responses + recorded
    classification (bucket/action) keyed by event_id, all read-only."""
    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = aiosqlite.Row

        where, params = [], []
        if event_id:
            where.append("id = ?")
            params.append(event_id)
        if channel:
            where.append("channel = ?")
            params.append(channel)
        sql = "SELECT id, channel, author, full_message_text, trigger_preview, " \
              "received_at FROM signal_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY received_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur = await conn.execute(sql, params)
        events = [dict(r) for r in await cur.fetchall()]

        # Recorded LLM responses keyed by message text (for the replay LLM), and
        # the recorded classification (bucket/action) keyed by event_id (for the
        # divergence check).
        cur = await conn.execute(
            "SELECT event_id, msg_text, llm_response_json, bucket, action_taken "
            "FROM classification_log")
        responses_by_text: dict[str, dict] = {}
        recorded_by_event: dict[str, dict] = {}
        for r in await cur.fetchall():
            if r["llm_response_json"]:
                try:
                    responses_by_text[r["msg_text"]] = json.loads(r["llm_response_json"])
                except json.JSONDecodeError:
                    pass
            recorded_by_event[r["event_id"]] = {
                "bucket": r["bucket"], "action": r["action_taken"],
            }
    return events, responses_by_text, recorded_by_event


def _short(s, n):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _divergence(result, recorded_by_event) -> str | None:
    """None if the replay matches the recorded classification, else a label."""
    rec = recorded_by_event.get(result.event_id)
    if rec is None:
        return None  # nothing recorded to compare against
    if (result.bucket or "") != (rec["bucket"] or ""):
        return f"bucket {rec['bucket']}→{result.bucket}"
    if (result.action_taken or "") != (rec["action"] or ""):
        return f"action {rec['action']}→{result.action_taken}"
    return None


def _order_summary(orders) -> str:
    if not orders:
        return "-"
    o = orders[0]
    extra = f" (+{len(orders) - 1})" if len(orders) > 1 else ""
    lp = "MKT" if o["limit_price"] is None else f"@{o['limit_price']:.2f}"
    return f"{o['action']} {o['quantity']} {o['instrument']} {o['order_type']}{lp}{extra}"


def _print_table(results, recorded_by_event) -> None:
    header = (f"{'EVENT':<12} {'CHANNEL':<18} {'BUCKET':<6} {'ACTION':<14} "
              f"{'STATUS':<10} {'WOULD-BE ORDER':<32} FLAGS")
    print(header)
    print("-" * len(header))
    for r in results:
        flags = []
        if not r.llm_recorded:
            flags.append("⚠no-llm")
        div = _divergence(r, recorded_by_event)
        if div:
            flags.append(f"DIVERGE[{div}]")
        print(f"{_short(r.event_id, 11):<12} {_short(r.channel, 17):<18} "
              f"{(r.bucket or '-'):<6} {(r.action_taken or '-'):<14} "
              f"{r.final_status:<10} {_order_summary(r.would_be_orders):<32} "
              f"{' '.join(flags)}")


def _print_summary(results, recorded_by_event) -> None:
    by_status: dict[str, int] = {}
    no_llm = 0
    diverged = 0
    orders = 0
    for r in results:
        by_status[r.final_status] = by_status.get(r.final_status, 0) + 1
        if not r.llm_recorded:
            no_llm += 1
        if _divergence(r, recorded_by_event):
            diverged += 1
        orders += len(r.would_be_orders)
    print()
    status_str = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
    print(f"SUMMARY: {len(results)} alerts | {status_str}")
    print(f"         would-be orders: {orders} | no recorded LLM: {no_llm} | "
          f"divergences: {diverged}")


async def _run(args) -> int:
    db_path = args.db
    if not os.path.exists(db_path):
        print(f"error: db not found: {db_path}", file=sys.stderr)
        return 2

    policy = load_policy(args.policy)
    events, responses_by_text, recorded_by_event = await _load(
        db_path, channel=args.channel, limit=args.limit, event_id=args.event_id)

    if not events:
        print("no matching signal_events", file=sys.stderr)

    recorded_llm = RecordedClassifierClient(responses_by_text)
    results = await replay_all(
        events, policy, recorded_llm, net_liq=args.net_liq, quote=args.quote)

    if args.json:
        payload = {
            "results": [
                {**asdict(r),
                 "divergence": _divergence(r, recorded_by_event)}
                for r in results
            ],
            "summary": {
                "alerts": len(results),
                "no_recorded_llm": sum(1 for r in results if not r.llm_recorded),
                "divergences": sum(
                    1 for r in results if _divergence(r, recorded_by_event)),
            },
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_table(results, recorded_by_event)
        _print_summary(results, recorded_by_event)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay captured alerts through the live chain (paper, no real orders).")
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--policy", default="config/policy.yaml")
    parser.add_argument("--channel", default=None, help="filter by channel")
    parser.add_argument("--limit", type=int, default=None, help="max alerts")
    parser.add_argument("--event-id", dest="event_id", default=None)
    parser.add_argument("--net-liq", dest="net_liq", type=float, default=100_000.0)
    parser.add_argument("--quote", type=float, default=100.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
