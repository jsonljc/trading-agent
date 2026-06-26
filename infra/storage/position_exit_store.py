from __future__ import annotations
import math
from datetime import datetime, timezone
import aiosqlite


def _round_half_up_min1(n: float) -> int:
    return max(1, int(math.floor(n + 0.5)))


class PositionExitStore:
    """Sell-following idempotency + the per-intent exit ledger.

    `sell_event_claims` dedups a sell EVENT by its message fingerprint (stable
    across reposts/edits). `position_exits` records each share lot sold so
    `remaining_qty` can net it (together with trim-ladder sells) against the
    original fill quantity.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def claim_sell_event(self, fingerprint: str, event_id: str) -> bool:
        """Atomically claim a sell event. Returns False if already claimed (a
        reposted/redelivered sell with the same content). Permanent — a rare
        RTH zero-fill is alerted for manual handling, never auto-retried."""
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO sell_event_claims (fingerprint, event_id, claimed_at) "
            "VALUES (?, ?, ?)",
            (fingerprint, event_id, now),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def release_sell_event(self, fingerprint: str) -> None:
        """Undo a claim when NOTHING was sold (e.g. broker down on the first
        order) so a repost can retry. Only safe to call when sold_qty == 0 for
        this event — otherwise the sold portion would be re-sold."""
        await self._conn.execute(
            "DELETE FROM sell_event_claims WHERE fingerprint=?", (fingerprint,))
        await self._conn.commit()

    async def record_exit(self, *, fingerprint: str, event_id: str | None,
                          intent_id: str, channel: str | None, ticker: str | None,
                          scope: str, requested_qty: int, sold_qty: int,
                          sold_avg_price: float | None, broker_order_ref: str | None,
                          reason: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO position_exits "
            "(fingerprint, event_id, intent_id, channel, ticker, scope, "
            " requested_qty, sold_qty, sold_avg_price, broker_order_ref, reason, "
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (fingerprint, event_id, intent_id, channel, ticker, scope,
             requested_qty, sold_qty, sold_avg_price, broker_order_ref, reason, now),
        )
        await self._conn.commit()

    async def reserve_exit(self, *, fingerprint: str, event_id: str | None,
                           intent_id: str, channel: str | None, ticker: str | None,
                           scope: str, requested_qty: int, reason: str | None) -> int:
        """Reserve an in-flight trader-sell of `requested_qty` shares BEFORE the
        order is placed. The row carries sold_qty=NULL (a pending reservation);
        remaining_qty counts requested_qty for it, so a concurrent trim (or a
        second-fingerprint sell) cannot size against shares this sell is already
        taking. Mirrors the trim ladder's in-flight reserve. Returns the row id;
        finalize_exit(id, ...) corrects it to the actual fill."""
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.execute(
            "INSERT INTO position_exits "
            "(fingerprint, event_id, intent_id, channel, ticker, scope, "
            " requested_qty, sold_qty, sold_avg_price, broker_order_ref, reason, "
            " created_at) VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
            (fingerprint, event_id, intent_id, channel, ticker, scope,
             requested_qty, reason, now),
        )
        await self._conn.commit()
        return int(cur.lastrowid)

    async def finalize_exit(self, exit_id: int, *, sold_qty: int,
                            sold_avg_price: float | None,
                            broker_order_ref: str | None,
                            reason: str | None) -> None:
        """Finalize a reserved exit with the ACTUAL fill. Setting sold_qty (even
        0) converts the pending reserve into a recorded exit: remaining_qty then
        counts the real sold_qty and releases any over-reserve (requested-sold).
        A finalize with sold_qty=0 fully releases the reserve (nothing sold)."""
        await self._conn.execute(
            "UPDATE position_exits "
            "SET sold_qty=?, sold_avg_price=?, broker_order_ref=?, reason=? "
            "WHERE id=?",
            (sold_qty, sold_avg_price, broker_order_ref, reason, exit_id),
        )
        await self._conn.commit()

    async def sold_qty_for_intent(self, intent_id: str) -> int:
        async with self._conn.execute(
            "SELECT COALESCE(SUM(sold_qty), 0) FROM position_exits WHERE intent_id=?",
            (intent_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0] or 0)

    async def remaining_qty(self, intent_id: str) -> int:
        """Shares still held for an intent = fill_qty − trims − exits, where
        BOTH trims and exits net recorded sells AND in-flight reserves (a claimed
        trim rung, or a placed-but-unrecorded trader-sell).

        Trims count recorded `sold_qty` for fired rungs AND RESERVE in-flight
        claimed-but-unrecorded rungs (fire_started_at set, fired_at NULL) at
        round(fill_qty × trim_pct) — closing the trim/sell race where a sell
        could otherwise compute remaining too high and oversell."""
        async with self._conn.execute(
            "SELECT fill_qty FROM trade_intents WHERE intent_id=?", (intent_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return 0
        fill_qty = int(row[0])

        trims_sold = 0
        async with self._conn.execute(
            "SELECT trim_pct, fired_at, fire_started_at, sold_qty "
            "FROM trade_intent_trims WHERE intent_id=?", (intent_id,)
        ) as cur:
            for trim_pct, fired_at, fire_started_at, sold_qty in await cur.fetchall():
                if sold_qty is not None:
                    trims_sold += int(sold_qty)               # recorded fill
                elif fire_started_at is not None and fired_at is None:
                    trims_sold += _round_half_up_min1(fill_qty * trim_pct)  # in-flight reserve

        # Exits net BOTH recorded sells (sold_qty) AND in-flight reserves
        # (sold_qty NULL -> reserve requested_qty), symmetric to the trim reserve
        # above. This closes the trim/sell race: while a trader-sell is placed-
        # but-unrecorded, remaining_qty already excludes the shares it is taking.
        async with self._conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN sold_qty IS NOT NULL THEN sold_qty "
            "ELSE COALESCE(requested_qty, 0) END), 0) "
            "FROM position_exits WHERE intent_id=?",
            (intent_id,),
        ) as cur:
            row = await cur.fetchone()
        exits_reserved = int(row[0] or 0)
        return max(0, fill_qty - trims_sold - exits_reserved)
