from __future__ import annotations

# Options are valued at premium x 100 (capital deployed), matching
# agent/pnl_attribution.OPTION_MULTIPLIER.
OPTION_MULTIPLIER = 100

# Net open deployed notional (cost basis) across all currently-open, filled long
# entries. Each filled long entry's still-held quantity (fill_qty minus
# trim-ladder fires and follow-sell exits) is valued at its entry fill price;
# options multiply by 100. All stores share ONE sqlite connection, so the
# trims/exits tables are read off the same connection the trade-intent store
# holds -- the phase2b chain builder is not handed their store objects, only
# trade_intent_store. Read-only; any error propagates so ExposureGuard fails
# safe.
_OPEN_NOTIONAL_SQL = """
SELECT COALESCE(SUM(
    MAX(
        ti.fill_qty
        - COALESCE((SELECT SUM(t.sold_qty) FROM trade_intent_trims t
                    WHERE t.intent_id = ti.intent_id), 0)
        - COALESCE((SELECT SUM(e.sold_qty) FROM position_exits e
                    WHERE e.intent_id = ti.intent_id), 0),
        0
    ) * ti.fill_price
      * (CASE WHEN ti.instrument_type = 'option' THEN 100 ELSE 1 END)
), 0.0)
FROM trade_intents ti
WHERE ti.execution_state = 'filled'
  AND LOWER(ti.side) = 'long'
  AND ti.fill_price IS NOT NULL
  AND ti.fill_qty IS NOT NULL
"""


async def open_deployed_notional(trade_intent_store) -> float:
    """Total capital currently deployed across open filled long positions.

    Reads the shared sqlite connection the trade-intent store holds. Closed lots
    (fully sold via trims + exits) net to zero. Raises on any DB error so the
    caller can fail safe rather than under-report exposure.
    """
    conn = trade_intent_store._conn
    async with conn.execute(_OPEN_NOTIONAL_SQL) as cur:
        row = await cur.fetchone()
    val = row[0] if row else 0.0
    return float(val) if val is not None else 0.0
