#!/usr/bin/env python3
"""Per-source realized P&L report. Read-only; never touches orders.

Realized P&L per trader/channel from existing stores (entries in trade_intents;
sells in trade_intent_trims + position_exits), with per-ticker / equity-vs-option
breakdowns and win-rate stats. See
docs/superpowers/specs/2026-06-02-per-source-pnl-attribution-design.md.

Usage:
    python bin/pnl_report.py
    python bin/pnl_report.py --channel stp
    python bin/pnl_report.py --since-entry 2026-05-01
    python bin/pnl_report.py --since-sell 2026-05-15
    python bin/pnl_report.py --telegram
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sqlite3
import sys

from agent.pnl_attribution import compute_attribution, AttributionReport
from agent.policy import load_policy
from infra.telegram.client import TelegramClient


def _safe_fetchall(conn, sql):
    """SELECT that tolerates a not-yet-migrated DB: a missing optional table
    (trade_intent_trims / position_exits on a legacy DB) yields no rows
    rather than crashing the read-only report."""
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def _fetch(db_path, *, channel=None, since_entry=None, since_sell=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = "execution_state='filled'"
        params: list = []
        if channel:
            where += " AND channel=?"
            params.append(channel)
        if since_entry:
            where += " AND filled_at>=?"
            params.append(since_entry)
        entries = conn.execute(
            "SELECT intent_id, channel, ticker, instrument_type, fill_price, "
            f"fill_qty FROM trade_intents WHERE {where}", params).fetchall()
        ids = {e["intent_id"] for e in entries}

        trims = [r for r in _safe_fetchall(
            conn,
            "SELECT intent_id, sold_qty, sold_avg_price, fired_at "
            "FROM trade_intent_trims WHERE fired_at IS NOT NULL")
            if r["intent_id"] in ids
            and (not since_sell or (r["fired_at"] or "") >= since_sell)]
        exits = [r for r in _safe_fetchall(
            conn,
            "SELECT intent_id, sold_qty, sold_avg_price, created_at "
            "FROM position_exits")
            if r["intent_id"] in ids
            and (not since_sell or (r["created_at"] or "") >= since_sell)]
    finally:
        conn.close()

    if since_sell:
        # In sell-window mode, only show lots that actually realized in-window.
        sold_ids = {r["intent_id"] for r in trims} | {r["intent_id"] for r in exits}
        entries = [e for e in entries if e["intent_id"] in sold_ids]
    return entries, trims, exits


def render_table(report: AttributionReport) -> str:
    if not report.sources:
        return "No realized P&L for the selected window."
    lines = []
    header = f"{'Source':<14} {'Realized':>12} {'Lots':>5} {'Win%':>6} " \
             f"{'AvgWin':>10} {'AvgLoss':>10}"
    lines.append(header)
    lines.append("-" * len(header))
    for s in report.sources:
        avg_win_str = f"{s.avg_win:>+10.2f}" if s.wins else f"{'-':>10}"
        avg_loss_str = f"{s.avg_loss:>+10.2f}" if s.losses else f"{'-':>10}"
        lines.append(
            f"{s.channel:<14} {s.realized:>+12.2f} {s.closed_lots:>5} "
            f"{s.win_rate * 100:>5.0f}% {avg_win_str} {avg_loss_str}")
        for l in s.by_ticker:
            lines.append(f"    {l.ticker:<10} ({l.instrument_type:<6}) "
                         f"{l.realized:>+12.2f}  [{l.closed_lots} closed]")
        if s.open_options:
            lines.append(f"    {s.open_options} open option lot(s), cost "
                         f"{s.open_option_cost:>.2f}  [open · no exit path]")
        for f in s.flags:
            lines.append(f"    ⚠ {f}")
    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':<14} {report.grand_total:>+12.2f} "
                 f"{report.total_closed_lots:>5} {report.win_rate * 100:>5.0f}%")
    return "\n".join(lines)


def render_telegram(report: AttributionReport) -> str:
    if not report.sources:
        return "<b>P&amp;L</b>: no realized P&amp;L for the selected window."
    rows = [f"<b>Realized P&amp;L by source</b>  (total "
            f"<b>{report.grand_total:+.2f}</b>, {report.total_closed_lots} lots, "
            f"{report.win_rate * 100:.0f}% win)"]
    for s in report.sources:
        rows.append(f"• <b>{s.channel}</b>: {s.realized:+.2f} "
                    f"({s.closed_lots} lots, {s.win_rate * 100:.0f}% win)")
    return "\n".join(rows)


async def _send_telegram(policy_path: str, text: str) -> None:
    policy = load_policy(policy_path)
    client = TelegramClient(policy.telegram.bot_token, policy.telegram.chat_id)
    await client.send_message(text)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Per-source realized P&L report")
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--channel", default=None)
    parser.add_argument("--since-entry", default=None,
                        help="ISO date; include lots whose entry filled on/after")
    parser.add_argument("--since-sell", default=None,
                        help="ISO date; realized from sells on/after (lot must "
                             "have an in-window sell to appear)")
    parser.add_argument("--policy", default="config/policy.yaml")
    parser.add_argument("--telegram", action="store_true",
                        help="also push a compact summary to Telegram")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2

    entries, trims, exits = _fetch(
        args.db, channel=args.channel, since_entry=args.since_entry,
        since_sell=args.since_sell)
    report = compute_attribution(entries, trims, exits)
    print(render_table(report))
    if args.telegram:
        try:
            asyncio.run(_send_telegram(args.policy, render_telegram(report)))
        except Exception as exc:  # report already printed; surface send failure
            print(f"error: telegram send failed: {exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
