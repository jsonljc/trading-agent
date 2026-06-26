"""§3a regression: a trim firing inside a trader-sell's in-flight (placed but
unrecorded) window must NOT oversell. follow_sell_position reserves the full
sell qty before place_order, so remaining_qty=0 during the window and the
concurrent trim short-circuits. Guards the fix in spec 2026-06-26 §3a.
"""
import aiosqlite
import pytest

from agent.exit_ladder import fire_rung_if_crossed
from infra.storage.db import SCHEMA
from infra.storage.position_exit_store import PositionExitStore
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from skills.execution.sell_follower import follow_sell_position
from tests.support.factories import make_filled_intent
from tests.support.fake_gateway import FakeGateway


@pytest.mark.asyncio
async def test_inflight_sell_concurrent_trim_does_not_oversell():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        intents = TradeIntentStore(conn)
        trims = TrimLadderStore(conn)
        exits = PositionExitStore(conn)

        fill_qty = 100
        intent_id = "e1:AAPL:long"
        await intents.insert(make_filled_intent(
            intent_id, channel="mystic", ticker="AAPL", fill_qty=fill_qty, seq=1))
        await trims.arm(intent_id, rungs=[(1, 0.05, 0.50)],
                        armed_at="2026-06-26T14:30:00+00:00")

        gw = FakeGateway()  # full fills

        # While the SELL is placed-but-unrecorded (inside wait_fill), fire the
        # trim through the REAL ladder path against the REAL remaining_qty.
        async def concurrent_trim():
            await fire_rung_if_crossed(
                gw=gw, trim_store=trims, exits_store=exits,
                intent_id=intent_id, ticker="AAPL", avg_fill_price=100.0,
                original_qty=fill_qty, rung=1, threshold_pct=0.05, trim_pct=0.50,
                current_price=106.0, slippage_cap_pct=0.01)
        gw.on_wait_fill = concurrent_trim

        # The trader sells the whole position (sized against remaining_qty=100).
        sold = await follow_sell_position(
            gw=gw, exits_store=exits, fingerprint="fp-1", event_id="evt-sell",
            intent_id=intent_id, channel="mystic", ticker="AAPL", qty=fill_qty,
            scope="full", slippage_cap_pct=0.01, fill_timeout=5.0)

        recorded_exit = await exits.sold_qty_for_intent(intent_id)
        recorded_trim = 0
        for r in await trims.all_for_intent(intent_id):
            recorded_trim += int(r["sold_qty"] or 0)
        total_recorded = recorded_exit + recorded_trim

        assert total_recorded <= fill_qty, (
            f"OVERSELL: recorded {total_recorded} (exit={recorded_exit} "
            f"trim={recorded_trim}) > fill {fill_qty}; sold returned {sold}")


@pytest.mark.asyncio
async def test_inflight_sell_concurrent_trim_during_prepare_does_not_oversell():
    """§3a closure: a trim firing during the sell's PREPARE window (inside
    qualify_equity, before place) must not oversell either. follow_sell_position
    reserves before any broker round-trip, so the concurrent trim reads
    remaining_qty=0 and short-circuits. If the reserve happened after
    qualify/quote, this would record 150 of 100."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        intents = TradeIntentStore(conn)
        trims = TrimLadderStore(conn)
        exits = PositionExitStore(conn)

        fill_qty = 100
        intent_id = "e1:AAPL:long"
        await intents.insert(make_filled_intent(
            intent_id, channel="mystic", ticker="AAPL", fill_qty=fill_qty, seq=1))
        await trims.arm(intent_id, rungs=[(1, 0.05, 0.50)],
                        armed_at="2026-06-26T14:30:00+00:00")

        gw = FakeGateway()  # full fills

        # Fire the trim through the REAL ladder path during the sell's prepare
        # step (inside qualify_equity), i.e. BEFORE the sell places its order.
        async def concurrent_trim():
            await fire_rung_if_crossed(
                gw=gw, trim_store=trims, exits_store=exits,
                intent_id=intent_id, ticker="AAPL", avg_fill_price=100.0,
                original_qty=fill_qty, rung=1, threshold_pct=0.05, trim_pct=0.50,
                current_price=106.0, slippage_cap_pct=0.01)
        gw.on_qualify = concurrent_trim

        sold = await follow_sell_position(
            gw=gw, exits_store=exits, fingerprint="fp-1", event_id="evt-sell",
            intent_id=intent_id, channel="mystic", ticker="AAPL", qty=fill_qty,
            scope="full", slippage_cap_pct=0.01, fill_timeout=5.0)

        recorded_exit = await exits.sold_qty_for_intent(intent_id)
        recorded_trim = 0
        for r in await trims.all_for_intent(intent_id):
            recorded_trim += int(r["sold_qty"] or 0)
        total_recorded = recorded_exit + recorded_trim

        assert total_recorded <= fill_qty, (
            f"OVERSELL: recorded {total_recorded} (exit={recorded_exit} "
            f"trim={recorded_trim}) > fill {fill_qty}; sold returned {sold}")
