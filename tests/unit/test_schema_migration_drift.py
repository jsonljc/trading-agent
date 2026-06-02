# tests/unit/test_schema_migration_drift.py
"""
Regression for the May-8 incident: SCHEMA in db.py grew columns
(fill_qty, parent_intent_id) but _migrate() was not updated, so live DBs
went stale and every options-sleeve insert crashed with
'OperationalError: table trade_intents has no column named parent_intent_id'.

This test simulates an old live DB by pre-creating trade_intents WITHOUT
the new columns, then runs get_connection() (which calls _migrate()), then
asserts the column set matches what the live SCHEMA defines.
"""
from __future__ import annotations
import re
import pytest
import aiosqlite
from infra.storage.db import get_connection, SCHEMA


def _expected_columns_from_schema(table: str) -> set[str]:
    """Parse columns out of the SCHEMA string for a given table."""
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\)",
        SCHEMA, re.DOTALL,
    )
    assert m, f"could not find {table} in SCHEMA"
    body = m.group(1)
    cols: set[str] = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        if line.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK")):
            continue
        # First token on the line is the column name.
        name = line.split()[0]
        cols.add(name)
    return cols


async def _live_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_migrate_brings_old_db_up_to_current_schema(tmp_path):
    db_path = tmp_path / "old.db"

    # Simulate a pre-fill_qty / pre-parent_intent_id DB.
    # Snapshot of trade_intents as it currently exists in the live production
    # DB: SCHEMA's columns plus three historical relics (walk_profile,
    # max_chase_pct, reprice_interval_ms) that were removed from SCHEMA in
    # commit fc6dc57 but cannot be dropped from existing SQLite tables. Minus
    # fill_qty + parent_intent_id, which is the drift this test catches.
    OLD_TRADE_INTENTS = """
    CREATE TABLE trade_intents (
        intent_id              TEXT PRIMARY KEY,
        event_id               TEXT NOT NULL,
        channel                TEXT NOT NULL,
        ticker                 TEXT NOT NULL,
        side                   TEXT NOT NULL,
        instrument_type        TEXT NOT NULL,
        expiry                 TEXT,
        strike                 REAL,
        right                  TEXT,
        conviction             TEXT NOT NULL,
        analysis_confidence    REAL,
        ambiguity_flags        TEXT,
        rationale              TEXT,
        ticker_raw             TEXT,
        side_raw               TEXT,
        conviction_raw         TEXT,
        reference_spot_price   REAL,
        reference_spot_timestamp TEXT,
        policy_state           TEXT NOT NULL,
        execution_mode         TEXT,
        order_type             TEXT,
        walk_profile           TEXT,
        initial_reference_ask  REAL,
        initial_order_limit    REAL,
        max_chase_pct          REAL,
        max_chase_price        REAL,
        max_reprices           INTEGER,
        reprice_interval_ms    INTEGER,
        execution_state        TEXT,
        outbox_status          TEXT,
        broker_order_ref       TEXT,
        order_attempt_count    INTEGER,
        last_limit_price       REAL,
        fill_price             REAL,
        dlq_reason             TEXT,
        cancel_reason          TEXT,
        signal_received_at     TEXT NOT NULL,
        intent_created_at      TEXT NOT NULL,
        order_submitted_at     TEXT,
        order_ack_at           TEXT,
        filled_at              TEXT,
        cancelled_at           TEXT,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL,
        partial_execution_reason TEXT
    );
    """
    pre = await aiosqlite.connect(str(db_path))
    try:
        await pre.executescript(OLD_TRADE_INTENTS)
        await pre.commit()
    finally:
        await pre.close()

    # Now open through the production code path (runs _migrate()).
    conn = await get_connection(str(db_path))
    try:
        live = await _live_columns(conn, "trade_intents")
        expected = _expected_columns_from_schema("trade_intents")
        missing = expected - live
        assert not missing, (
            f"_migrate() did not add columns expected by SCHEMA: {sorted(missing)}. "
            f"Add _add_column_if_missing(...) calls in infra/storage/db.py::_migrate."
        )
    finally:
        await conn.close()
