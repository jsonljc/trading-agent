from __future__ import annotations
import aiosqlite


class TrimLadderStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def arm(self, intent_id: str, *, rungs: list[tuple[int, float, float]],
                  armed_at: str) -> None:
        for rung, threshold_pct, trim_pct in rungs:
            await self._conn.execute(
                "INSERT INTO trade_intent_trims "
                "(intent_id, rung, threshold_pct, trim_pct, armed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (intent_id, rung, threshold_pct, trim_pct, armed_at),
            )
        await self._conn.commit()

    async def record_fire(self, *, intent_id: str, rung: int, fired_at: str,
                          fire_price: float, sold_qty: int,
                          sold_avg_price: float | None,
                          broker_order_ref: str | None) -> None:
        await self._conn.execute(
            "UPDATE trade_intent_trims "
            "SET fired_at=?, fire_price=?, sold_qty=?, sold_avg_price=?, broker_order_ref=? "
            "WHERE intent_id=? AND rung=?",
            (fired_at, fire_price, sold_qty, sold_avg_price, broker_order_ref,
             intent_id, rung),
        )
        await self._conn.commit()

    async def unfired_for_intent(self, intent_id: str) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT * FROM trade_intent_trims WHERE intent_id=? AND fired_at IS NULL "
            "ORDER BY rung",
            (intent_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def all_for_intent(self, intent_id: str) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT * FROM trade_intent_trims WHERE intent_id=? ORDER BY rung",
            (intent_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def all_unfired(self) -> list[aiosqlite.Row]:
        # Excludes rungs already claimed by a prior tick (fire_started_at set
        # but fired_at not yet recorded). Prevents the 2s polling cadence from
        # racing the >2s broker round-trip and double-firing the same rung.
        async with self._conn.execute(
            "SELECT * FROM trade_intent_trims "
            "WHERE fired_at IS NULL AND fire_started_at IS NULL "
            "ORDER BY intent_id, rung",
        ) as cur:
            return list(await cur.fetchall())

    async def claim_for_fire(self, intent_id: str, rung: int,
                             started_at: str) -> bool:
        """Atomically mark a rung as in-flight. Returns False if another
        tick already claimed it or it has fired."""
        cur = await self._conn.execute(
            "UPDATE trade_intent_trims SET fire_started_at=? "
            "WHERE intent_id=? AND rung=? "
            "AND fired_at IS NULL AND fire_started_at IS NULL",
            (started_at, intent_id, rung),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def release_claim(self, intent_id: str, rung: int) -> None:
        """Release an in-flight claim (e.g., broker unavailable) so the next
        tick can retry."""
        await self._conn.execute(
            "UPDATE trade_intent_trims SET fire_started_at=NULL "
            "WHERE intent_id=? AND rung=? AND fired_at IS NULL",
            (intent_id, rung),
        )
        await self._conn.commit()
