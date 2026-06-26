import pytest
import aiosqlite
from datetime import datetime, timezone
from infra.storage.db import SCHEMA
from infra.storage.trade_intent_store import TradeIntentStore
from skills.risk.exposure import open_deployed_notional


@pytest.fixture
async def store():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    yield TradeIntentStore(conn)
    await conn.close()


async def _write_entry(store, *, intent_id, ticker, instrument_type, side,
                       fill_price, fill_qty, execution_state="filled"):
    now = datetime.now(timezone.utc).isoformat()
    await store.insert({
        "intent_id": intent_id, "event_id": "e", "channel": "mystic",
        "ticker": ticker, "side": side, "instrument_type": instrument_type,
        "conviction": "HIGH", "fill_price": fill_price, "fill_qty": fill_qty,
        "execution_state": execution_state, "policy_state": "approved",
        "signal_received_at": now, "intent_created_at": now,
        "created_at": now, "updated_at": now,
        "filled_at": now if execution_state == "filled" else None,
    })


@pytest.mark.asyncio
async def test_empty_is_zero(store):
    assert await open_deployed_notional(store) == 0.0


@pytest.mark.asyncio
async def test_sums_equity_and_option_with_multiplier(store):
    # equity 100 * 50 = 5,000; option 2 * 10 * 100 = 2,000 -> 7,000
    await _write_entry(store, intent_id="a", ticker="AAA", instrument_type="equity",
                       side="long", fill_price=100.0, fill_qty=50)
    await _write_entry(store, intent_id="b", ticker="BBB", instrument_type="option",
                       side="long", fill_price=2.0, fill_qty=10)
    assert await open_deployed_notional(store) == pytest.approx(7_000.0)


@pytest.mark.asyncio
async def test_excludes_unfilled_and_non_long(store):
    await _write_entry(store, intent_id="a", ticker="AAA", instrument_type="equity",
                       side="long", fill_price=100.0, fill_qty=50,
                       execution_state="submitted")
    await _write_entry(store, intent_id="b", ticker="BBB", instrument_type="equity",
                       side="short", fill_price=100.0, fill_qty=50)
    assert await open_deployed_notional(store) == 0.0


@pytest.mark.asyncio
async def test_nets_trims_and_exits(store):
    # 100 sh @ $10 = $1,000 gross; trim 30 + exit 20 -> 50 held -> $500
    await _write_entry(store, intent_id="a", ticker="AAA", instrument_type="equity",
                       side="long", fill_price=10.0, fill_qty=100)
    conn = store._conn
    await conn.execute(
        "INSERT INTO trade_intent_trims "
        "(intent_id, rung, threshold_pct, trim_pct, armed_at, sold_qty) "
        "VALUES ('a', 1, 0.05, 0.40, 't', 30)")
    await conn.execute(
        "INSERT INTO position_exits (fingerprint, intent_id, sold_qty, created_at) "
        "VALUES ('fp', 'a', 20, 't')")
    await conn.commit()
    assert await open_deployed_notional(store) == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_fully_sold_contributes_zero(store):
    await _write_entry(store, intent_id="a", ticker="AAA", instrument_type="equity",
                       side="long", fill_price=10.0, fill_qty=100)
    conn = store._conn
    await conn.execute(
        "INSERT INTO position_exits (fingerprint, intent_id, sold_qty, created_at) "
        "VALUES ('fp', 'a', 100, 't')")
    await conn.commit()
    assert await open_deployed_notional(store) == 0.0


@pytest.mark.asyncio
async def test_pending_reserve_does_not_reduce_exposure(store):
    # An in-flight sell reserve (sold_qty NULL) is not yet sold: the shares are
    # still held, so open notional must still count them. This is intentionally
    # asymmetric with remaining_qty (which reserves the in-flight sell to block an
    # oversell) -- exposure measures capital still at risk, not sell-ability.
    await _write_entry(store, intent_id="a", ticker="AAA", instrument_type="equity",
                       side="long", fill_price=10.0, fill_qty=100)
    conn = store._conn
    await conn.execute(
        "INSERT INTO position_exits "
        "(fingerprint, intent_id, requested_qty, sold_qty, created_at) "
        "VALUES ('fp', 'a', 40, NULL, 't')")
    await conn.commit()
    # 100 still held -> $1,000 (SUM(sold_qty) ignores the NULL pending row).
    assert await open_deployed_notional(store) == pytest.approx(1_000.0)
