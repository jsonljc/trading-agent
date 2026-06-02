import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_events (
    id TEXT PRIMARY KEY,
    source TEXT,
    channel TEXT,
    author TEXT,
    trigger_preview TEXT,
    full_message_text TEXT,
    capture_mode TEXT,
    message_fingerprint TEXT,
    received_at TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    event_id TEXT,
    ticker TEXT,
    action TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS work_traces (
    trace_id TEXT PRIMARY KEY,
    event_id TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS skill_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    skill_name TEXT,
    status TEXT,
    output_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS option_candidates (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL,
    spread_pct REAL,
    open_interest INTEGER,
    volume INTEGER,
    multiplier INTEGER DEFAULT 100,
    contract_ref_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_artifacts (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT,
    signal_hash TEXT NOT NULL,
    approved_execution_mode TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    ticker TEXT NOT NULL,
    contract_ref_json TEXT,
    quantity INTEGER,
    notional_estimate REAL,
    limit_price REAL,
    sizing_reason TEXT,
    capped_by TEXT,
    broker_order_id TEXT,
    perm_id INTEGER,
    status TEXT NOT NULL,
    filled_qty INTEGER DEFAULT 0,
    avg_fill_price REAL,
    idempotency_key TEXT NOT NULL,
    submitted_at TEXT,
    filled_at TEXT,
    last_known_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_audit_log (
    id TEXT PRIMARY KEY,
    execution_id TEXT,
    signal_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    ctx_snapshot_json TEXT NOT NULL,
    pipeline_outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_intents (
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
    initial_reference_ask  REAL,
    initial_order_limit    REAL,
    max_chase_price        REAL,
    max_reprices           INTEGER,
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
    fill_qty               INTEGER,
    parent_intent_id       TEXT,
    partial_execution_reason TEXT
);
CREATE TABLE IF NOT EXISTS trade_intent_trims (
    intent_id            TEXT NOT NULL,
    rung                 INTEGER NOT NULL,
    threshold_pct        REAL NOT NULL,
    trim_pct             REAL NOT NULL,
    armed_at             TEXT NOT NULL,
    fire_started_at      TEXT,
    fired_at             TEXT,
    fire_price           REAL,
    sold_qty             INTEGER,
    sold_avg_price       REAL,
    broker_order_ref     TEXT,
    PRIMARY KEY (intent_id, rung),
    FOREIGN KEY (intent_id) REFERENCES trade_intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_intent_trims_unfired
    ON trade_intent_trims(intent_id) WHERE fired_at IS NULL;
CREATE VIEW IF NOT EXISTS dlq_intents AS
    SELECT * FROM trade_intents
    WHERE execution_state = 'failed'
    ORDER BY created_at DESC;
CREATE TABLE IF NOT EXISTS classification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    trader_handle TEXT NOT NULL,
    msg_text TEXT NOT NULL,
    features_json TEXT NOT NULL,
    llm_response_json TEXT,
    bucket TEXT NOT NULL,
    confidence REAL NOT NULL,
    size_pct REAL NOT NULL,
    size_source TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    reason TEXT,
    ticker TEXT,
    side TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_classification_log_trader_time
    ON classification_log(trader_handle, created_at);
-- idx_classification_log_dedup on (trader_handle, ticker, side, action_taken,
-- created_at) is created in _migrate() AFTER the ticker/side columns are
-- added — putting it here breaks bootstrap on legacy DBs whose pre-existing
-- table lacks those columns (CREATE TABLE IF NOT EXISTS is a no-op).

CREATE TABLE IF NOT EXISTS trader_examples_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_handle TEXT NOT NULL,
    msg_text TEXT NOT NULL,
    proposed_bucket TEXT NOT NULL,
    proposed_why TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_bucket TEXT
);

CREATE TABLE IF NOT EXISTS trader_state (
    trader_handle TEXT PRIMARY KEY,
    unavailable_until TEXT,
    updated_at TEXT NOT NULL
);
"""


async def get_connection(db_path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    # Explicit + extra headroom over the sqlite connect() default of 5000ms, so a
    # second connection (audit/promote script) during a live trade retries
    # rather than erroring instantly with "database is locked".
    await conn.execute("PRAGMA busy_timeout=10000")
    await conn.executescript(SCHEMA)
    await _migrate(conn)
    await conn.commit()
    return conn


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Idempotent ALTERs for columns added after first deploy. SQLite's
    CREATE TABLE IF NOT EXISTS does not patch existing tables."""
    await _add_column_if_missing(conn, "trade_intents", "partial_execution_reason", "TEXT")
    await _add_column_if_missing(conn, "trade_intent_trims", "fire_started_at", "TEXT")
    await _add_column_if_missing(conn, "trade_intents", "fill_qty", "INTEGER")
    await _add_column_if_missing(conn, "trade_intents", "parent_intent_id", "TEXT")
    # SameDayDedupGate queries by (trader, ticker, side) — denormalize from
    # the JSON blob so we can index it.
    await _add_column_if_missing(conn, "classification_log", "ticker", "TEXT")
    await _add_column_if_missing(conn, "classification_log", "side", "TEXT")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_classification_log_dedup "
        "ON classification_log(trader_handle, ticker, side, action_taken, created_at)"
    )


async def _add_column_if_missing(conn: aiosqlite.Connection, table: str,
                                 column: str, decl: str) -> None:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
