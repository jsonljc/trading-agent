# Spec 1 — Reliability & TradeIntent Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every signal a durable `trade_intents` row from post-parse intent creation through fill or terminal state, with channel policy and cooldown guards enforcing two independent state tracks.

**Architecture:** A new `trade_intents` table is the canonical record for both policy denials and execution outcomes. `TradeIntentWriter` creates one row per signal at Phase 2b entry. `ChannelPolicyGuard` and `CooldownGuard` are prepended to the Phase 2b chain and update the row to terminal policy states when blocking. `ExecutionReconciler` is extended to also scan for outbox-stuck intents.

**Tech Stack:** Python 3.12+, aiosqlite, Pydantic v2, pytest-asyncio, existing `agent/` and `skills/` patterns.

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `infra/storage/db.py` | Modify | Add `trade_intents` table + `dlq_intents` view to SCHEMA |
| `infra/storage/trade_intent_store.py` | Create | CRUD for `trade_intents` rows |
| `agent/policy.py` | Modify | Add `ChannelConfig`, update `CooldownPolicy`, change `watched_channels` type |
| `config/policy.yaml` | Modify | Convert `watched_channels` to dict, add `auto_execute`, add `cooldown_minutes` |
| `skills/execution/trade_intent_writer.py` | Create | Phase 2b entry skill: creates intent row |
| `skills/execution/channel_policy_guard.py` | Create | Checks channel `auto_execute` flag |
| `skills/execution/cooldown_guard.py` | Create | Checks per-ticker cooldown against `trade_intents` fills |
| `skills/execution/execution_audit_writer.py` | Modify | Add `update_intent_outbox_status()` method |
| `skills/execution/execution_reconciler.py` | Modify | Also scan `trade_intents` for pending/dispatched outbox rows |
| `agent/registry.py` | Modify | Prepend 3 new skills to phase2b chain |
| `main.py` | Modify | Instantiate `TradeIntentStore`, pass to new skills |
| `tests/unit/test_policy.py` | Modify | Update watched_channels fixture format |
| `tests/integration/test_trade_intent_store.py` | Create | Integration tests for store CRUD |
| `tests/unit/test_trade_intent_writer.py` | Create | Unit tests for TradeIntentWriter |
| `tests/unit/test_channel_policy_guard.py` | Create | Unit tests for ChannelPolicyGuard |
| `tests/unit/test_cooldown_guard.py` | Create | Unit tests for CooldownGuard |
| `tests/e2e/test_phase2b_execution_pipeline.py` | Modify | Assert intent row created + policy state |

---

## Task 1: DB Migration — trade_intents table + dlq_intents view

**Files:**
- Modify: `infra/storage/db.py`
- Test: `tests/integration/test_storage.py` (existing)

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_storage.py`:

```python
@pytest.mark.asyncio
async def test_trade_intents_table_exists(db):
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_intents'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None

@pytest.mark.asyncio
async def test_dlq_intents_view_exists(db):
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name='dlq_intents'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/integration/test_storage.py::test_trade_intents_table_exists tests/integration/test_storage.py::test_dlq_intents_view_exists -v
```

Expected: FAIL — tables not yet defined.

- [ ] **Step 3: Add trade_intents and dlq_intents to db.py SCHEMA**

In `infra/storage/db.py`, append to the `SCHEMA` string before the closing `"""`:

```python
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
    updated_at             TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS dlq_intents AS
    SELECT * FROM trade_intents
    WHERE execution_state = 'failed'
    ORDER BY created_at DESC;
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/integration/test_storage.py::test_trade_intents_table_exists tests/integration/test_storage.py::test_dlq_intents_view_exists -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/storage/db.py tests/integration/test_storage.py
git commit -m "feat(db): add trade_intents table and dlq_intents view"
```

---

## Task 2: TradeIntentStore

**Files:**
- Create: `infra/storage/trade_intent_store.py`
- Create: `tests/integration/test_trade_intent_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_trade_intent_store.py`:

```python
import pytest
from datetime import datetime, timezone
from infra.storage.trade_intent_store import TradeIntentStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _base_intent(intent_id="evt1:NVDA:long"):
    now = _now()
    return {
        "intent_id": intent_id,
        "event_id": "evt1",
        "channel": "mystic",
        "ticker": "NVDA",
        "side": "long",
        "instrument_type": "option",
        "conviction": "high",
        "policy_state": "approved",
        "signal_received_at": now,
        "intent_created_at": now,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_insert_and_get(db):
    store = TradeIntentStore(db)
    intent = _base_intent()
    await store.insert(intent)
    row = await store.get("evt1:NVDA:long")
    assert row["ticker"] == "NVDA"
    assert row["policy_state"] == "approved"


@pytest.mark.asyncio
async def test_update_policy_state(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    await store.update_policy_state("evt1:NVDA:long", "channel_blocked")
    row = await store.get("evt1:NVDA:long")
    assert row["policy_state"] == "channel_blocked"


@pytest.mark.asyncio
async def test_update_execution_state(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    now = _now()
    await store.update_execution_state(
        "evt1:NVDA:long",
        execution_state="filled",
        fill_price=5.25,
        filled_at=now,
        outbox_status="confirmed",
    )
    row = await store.get("evt1:NVDA:long")
    assert row["execution_state"] == "filled"
    assert row["fill_price"] == pytest.approx(5.25)
    assert row["outbox_status"] == "confirmed"


@pytest.mark.asyncio
async def test_update_outbox_status(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    await store.update_outbox_status("evt1:NVDA:long", "pending")
    row = await store.get("evt1:NVDA:long")
    assert row["outbox_status"] == "pending"


@pytest.mark.asyncio
async def test_get_filled_since(db):
    store = TradeIntentStore(db)
    now = _now()
    filled_intent = {**_base_intent("evt2:NVDA:long"), "event_id": "evt2"}
    await store.insert(filled_intent)
    await store.update_execution_state(
        "evt2:NVDA:long",
        execution_state="filled",
        filled_at=now,
        fill_price=5.0,
        outbox_status="confirmed",
    )
    rows = await store.get_filled_since("NVDA", "2020-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "NVDA"


@pytest.mark.asyncio
async def test_get_pending_outbox(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent())
    await store.update_outbox_status("evt1:NVDA:long", "pending")
    rows = await store.get_pending_outbox()
    assert len(rows) == 1
    assert rows[0]["outbox_status"] == "pending"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/integration/test_trade_intent_store.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement TradeIntentStore**

Create `infra/storage/trade_intent_store.py`:

```python
from __future__ import annotations
from datetime import datetime, timezone
import aiosqlite


class TradeIntentStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, record: dict) -> None:
        cols = ", ".join(record.keys())
        placeholders = ", ".join(f":{k}" for k in record.keys())
        await self._conn.execute(
            f"INSERT OR IGNORE INTO trade_intents ({cols}) VALUES ({placeholders})",
            record,
        )
        await self._conn.commit()

    async def get(self, intent_id: str) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM trade_intents WHERE intent_id = ?", (intent_id,)
        ) as cur:
            return await cur.fetchone()

    async def update_policy_state(self, intent_id: str, policy_state: str) -> None:
        await self._conn.execute(
            "UPDATE trade_intents SET policy_state=?, updated_at=? WHERE intent_id=?",
            (policy_state, datetime.now(timezone.utc).isoformat(), intent_id),
        )
        await self._conn.commit()

    async def update_execution_state(
        self,
        intent_id: str,
        execution_state: str,
        fill_price: float | None = None,
        filled_at: str | None = None,
        cancelled_at: str | None = None,
        cancel_reason: str | None = None,
        dlq_reason: str | None = None,
        outbox_status: str | None = None,
        broker_order_ref: str | None = None,
        order_attempt_count: int | None = None,
        last_limit_price: float | None = None,
        order_submitted_at: str | None = None,
        order_ack_at: str | None = None,
        initial_reference_ask: float | None = None,
        initial_order_limit: float | None = None,
        max_chase_pct: float | None = None,
        max_chase_price: float | None = None,
        walk_profile: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        fields = {"execution_state": execution_state, "updated_at": now}
        if fill_price is not None:
            fields["fill_price"] = fill_price
        if filled_at is not None:
            fields["filled_at"] = filled_at
        if cancelled_at is not None:
            fields["cancelled_at"] = cancelled_at
        if cancel_reason is not None:
            fields["cancel_reason"] = cancel_reason
        if dlq_reason is not None:
            fields["dlq_reason"] = dlq_reason
        if outbox_status is not None:
            fields["outbox_status"] = outbox_status
        if broker_order_ref is not None:
            fields["broker_order_ref"] = broker_order_ref
        if order_attempt_count is not None:
            fields["order_attempt_count"] = order_attempt_count
        if last_limit_price is not None:
            fields["last_limit_price"] = last_limit_price
        if order_submitted_at is not None:
            fields["order_submitted_at"] = order_submitted_at
        if order_ack_at is not None:
            fields["order_ack_at"] = order_ack_at
        if initial_reference_ask is not None:
            fields["initial_reference_ask"] = initial_reference_ask
        if initial_order_limit is not None:
            fields["initial_order_limit"] = initial_order_limit
        if max_chase_pct is not None:
            fields["max_chase_pct"] = max_chase_pct
        if max_chase_price is not None:
            fields["max_chase_price"] = max_chase_price
        if walk_profile is not None:
            fields["walk_profile"] = walk_profile
        set_clause = ", ".join(f"{k}=:{k}" for k in fields)
        await self._conn.execute(
            f"UPDATE trade_intents SET {set_clause} WHERE intent_id=:_id",
            {**fields, "_id": intent_id},
        )
        await self._conn.commit()

    async def update_outbox_status(self, intent_id: str, outbox_status: str) -> None:
        await self._conn.execute(
            "UPDATE trade_intents SET outbox_status=?, updated_at=? WHERE intent_id=?",
            (outbox_status, datetime.now(timezone.utc).isoformat(), intent_id),
        )
        await self._conn.commit()

    async def get_filled_since(self, ticker: str, since: str) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            """SELECT * FROM trade_intents
               WHERE ticker=? AND execution_state='filled' AND filled_at >= ?""",
            (ticker, since),
        ) as cur:
            return await cur.fetchall()

    async def get_pending_outbox(self) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT * FROM trade_intents WHERE outbox_status IN ('pending', 'dispatched')"
        ) as cur:
            return await cur.fetchall()
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/integration/test_trade_intent_store.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/storage/trade_intent_store.py tests/integration/test_trade_intent_store.py
git commit -m "feat(storage): add TradeIntentStore with CRUD for trade_intents"
```

---

## Task 3: Policy Model Updates + policy.yaml

**Files:**
- Modify: `agent/policy.py`
- Modify: `config/policy.yaml`
- Modify: `tests/unit/test_policy.py`

- [ ] **Step 1: Write failing tests**

Open `tests/unit/test_policy.py`. Find any test that uses `watched_channels: ["mystic"]` and note where it needs updating. Add a new test:

```python
def test_channel_config_auto_execute():
    raw = """
trigger:
  action_words: ["long"]
instrument_policy:
  prefer_options: true
  min_expiry_days: 180
  strike_policy: closest_itm_call
  fallback_to_stock_if_no_options: true
pricing_policy:
  mode: cheapest_fillable_limit
  option_spread_fraction: 0.25
  stock_buffer_pct: 0.001
sizing_policy:
  low_conviction_pct: 0.05
  high_conviction_pct: 0.10
market_hours:
  options_rth_only: true
  stock_premarket_allowed: true
  stock_premarket_start: "04:00"
  rth_start: "09:30"
  rth_end: "16:00"
  stock_afterhours_queue: true
cooldown_policy:
  enabled: true
  cooldown_minutes: 30
dedupe_policy:
  enabled: true
  key: message_fingerprint_plus_ticker_plus_action_plus_window
pricing_policy_guards:
  min_bid: 0.01
  max_spread_pct: 0.40
models:
  vision: claude-opus-4-7
  text: claude-haiku-4-5-20251001
watched_channels:
  mystic:
    auto_execute: true
  chat:
    auto_execute: false
discord_bundle_id: "com.hnc.Discord"
telegram:
  chat_id: "123"
  bot_token: "fake"
"""
    import yaml
    from agent.policy import PolicyModel
    policy = PolicyModel.model_validate(yaml.safe_load(raw))
    assert policy.watched_channels["mystic"].auto_execute is True
    assert policy.watched_channels["chat"].auto_execute is False
    assert policy.cooldown_policy.cooldown_minutes == 30
```

- [ ] **Step 2: Run failing test**

```
pytest tests/unit/test_policy.py::test_channel_config_auto_execute -v
```

Expected: FAIL — ChannelConfig not defined, watched_channels type mismatch.

- [ ] **Step 3: Update agent/policy.py**

Replace the `CooldownPolicy` class and add `ChannelConfig`, then update `watched_channels` type in `PolicyModel`:

```python
class ChannelConfig(BaseModel):
    auto_execute: bool = False


class CooldownPolicy(BaseModel):
    enabled: bool
    cooldown_minutes: int = 30
```

In `PolicyModel`, change:

```python
    watched_channels: dict[str, ChannelConfig]
```

(was: `watched_channels: list[str]`)

- [ ] **Step 4: Update config/policy.yaml**

Replace the `watched_channels` list and update `cooldown_policy`:

```yaml
cooldown_policy:
  enabled: true
  cooldown_minutes: 30

watched_channels:
  mystic:
    auto_execute: true
  alerts:
    auto_execute: true
  trades:
    auto_execute: true
  wall-st-engine:
    auto_execute: false
  stock-talk-portfolio:
    auto_execute: false
  chat:
    auto_execute: false
  yonezu:
    auto_execute: true
  pup-danny:
    auto_execute: true
  urkel:
    auto_execute: true
  gladiator:
    auto_execute: true
  graddox:
    auto_execute: true
  phat:
    auto_execute: true
  grid:
    auto_execute: true
```

- [ ] **Step 5: Fix existing watched_channels tests in test_policy.py**

Find any test that has `watched_channels: ["mystic"]` and update to:

```yaml
watched_channels:
  mystic:
    auto_execute: true
```

- [ ] **Step 6: Run all policy tests**

```
pytest tests/unit/test_policy.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add agent/policy.py config/policy.yaml tests/unit/test_policy.py
git commit -m "feat(policy): add ChannelConfig with auto_execute, add cooldown_minutes"
```

---

## Task 4: TradeIntentWriter Skill

**Files:**
- Create: `skills/execution/trade_intent_writer.py`
- Create: `tests/unit/test_trade_intent_writer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_trade_intent_writer.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.trade_intent_writer import TradeIntentWriter


def _store():
    s = MagicMock()
    s.insert = AsyncMock()
    return s


def _ctx(event_id="evt1", channel="mystic", ticker="NVDA",
         intent="LONG_SIGNAL", conviction_bucket="high",
         received_at="2026-04-24T10:00:00+00:00"):
    ctx = Context(trace_id="t1", event_id=event_id)
    ctx.update({
        "channel": channel,
        "ticker": ticker,
        "intent": intent,
        "conviction_bucket": conviction_bucket,
        "received_at": received_at,
    })
    return ctx


@pytest.mark.asyncio
async def test_creates_intent_row_and_sets_intent_id():
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = _ctx()
    result = await skill.run(ctx)
    assert result.status == "success"
    assert ctx.get("intent_id") == "evt1:NVDA:long"
    store.insert.assert_called_once()
    record = store.insert.call_args[0][0]
    assert record["ticker"] == "NVDA"
    assert record["side"] == "long"
    assert record["conviction"] == "high"
    assert record["channel"] == "mystic"
    assert record["policy_state"] == "approved"
    assert record["execution_state"] is None


@pytest.mark.asyncio
async def test_add_signal_maps_to_long():
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = _ctx(intent="ADD_SIGNAL")
    await skill.run(ctx)
    record = store.insert.call_args[0][0]
    assert record["side"] == "long"


@pytest.mark.asyncio
async def test_uses_side_key_if_set_by_signal_analyzer():
    """SignalAnalyzer (Spec 2) sets 'side' directly; TradeIntentWriter prefers it."""
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = _ctx()
    ctx.update({"side": "short"})
    await skill.run(ctx)
    record = store.insert.call_args[0][0]
    assert record["side"] == "short"
    assert ctx.get("intent_id") == "evt1:NVDA:short"


@pytest.mark.asyncio
async def test_missing_ticker_returns_fail():
    store = _store()
    skill = TradeIntentWriter(store)
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": "mystic", "intent": "LONG_SIGNAL", "conviction_bucket": "high",
                "received_at": "2026-04-24T10:00:00+00:00"})
    result = await skill.run(ctx)
    assert result.status == "fail"
    store.insert.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_trade_intent_writer.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement TradeIntentWriter**

Create `skills/execution/trade_intent_writer.py`:

```python
from __future__ import annotations
import logging
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class TradeIntentWriter(Skill):
    name = "TradeIntentWriter"

    def __init__(self, trade_intent_store) -> None:
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        if not ticker:
            return SkillResult(status="fail", reason="trade_intent_writer: ticker missing from context")

        # Prefer 'side' (set by SignalAnalyzer in Spec 2); fall back to deriving from Phase 1 intent
        side = ctx.get("side")
        if not side:
            phase1_intent = ctx.get("intent", "")
            side = "long" if phase1_intent in ("LONG_SIGNAL", "ADD_SIGNAL") else "long"

        # Prefer 'conviction' (set by SignalAnalyzer); fall back to conviction_bucket from Phase 1
        conviction = ctx.get("conviction") or ctx.get("conviction_bucket", "medium")

        now = datetime.now(timezone.utc).isoformat()
        intent_id = f"{ctx.event_id}:{ticker}:{side}"

        record = {
            "intent_id": intent_id,
            "event_id": ctx.event_id,
            "channel": ctx.get("channel", ""),
            "ticker": ticker,
            "side": side,
            "instrument_type": "option",  # confirmed after ChainLookup; default option
            "expiry": None,
            "strike": None,
            "right": None,
            "conviction": conviction,
            "analysis_confidence": ctx.get("analysis_confidence"),
            "ambiguity_flags": ctx.get("ambiguity_flags"),
            "rationale": ctx.get("reason"),
            "ticker_raw": ctx.get("ticker_raw", ticker),
            "side_raw": ctx.get("side_raw") or ctx.get("intent"),
            "conviction_raw": ctx.get("conviction_raw") or ctx.get("conviction_bucket"),
            "reference_spot_price": None,
            "reference_spot_timestamp": None,
            "policy_state": "approved",
            "execution_mode": None,
            "execution_state": None,
            "outbox_status": None,
            "signal_received_at": ctx.get("received_at", now),
            "intent_created_at": now,
            "created_at": now,
            "updated_at": now,
        }

        await self._store.insert(record)
        logger.info("TradeIntentWriter: created intent %s for %s/%s", intent_id, ticker, side)
        return SkillResult(status="success", updates={"intent_id": intent_id})
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_trade_intent_writer.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/trade_intent_writer.py tests/unit/test_trade_intent_writer.py
git commit -m "feat(execution): add TradeIntentWriter skill"
```

---

## Task 5: ChannelPolicyGuard Skill

**Files:**
- Create: `skills/execution/channel_policy_guard.py`
- Create: `tests/unit/test_channel_policy_guard.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_channel_policy_guard.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.channel_policy_guard import ChannelPolicyGuard


def _policy(auto_execute: bool, channel: str = "mystic"):
    ch_cfg = MagicMock()
    ch_cfg.auto_execute = auto_execute
    p = MagicMock()
    p.watched_channels = {channel: ch_cfg}
    return p


def _store():
    s = MagicMock()
    s.update_policy_state = AsyncMock()
    return s


def _ctx(channel: str = "mystic", intent_id: str = "evt1:NVDA:long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": channel, "intent_id": intent_id})
    return ctx


@pytest.mark.asyncio
async def test_auto_execute_true_passes():
    skill = ChannelPolicyGuard(_policy(auto_execute=True), _store())
    result = await skill.run(_ctx())
    assert result.status == "success"


@pytest.mark.asyncio
async def test_auto_execute_false_blocks():
    store = _store()
    skill = ChannelPolicyGuard(_policy(auto_execute=False), store)
    ctx = _ctx()
    result = await skill.run(ctx)
    assert result.status == "skip"
    assert "channel_blocked" in result.reason
    store.update_policy_state.assert_called_once_with("evt1:NVDA:long", "channel_blocked")


@pytest.mark.asyncio
async def test_unknown_channel_blocks():
    store = _store()
    skill = ChannelPolicyGuard(_policy(auto_execute=True, channel="mystic"), store)
    ctx = _ctx(channel="unknown-channel")
    result = await skill.run(ctx)
    assert result.status == "skip"
    store.update_policy_state.assert_called_once_with("evt1:NVDA:long", "channel_blocked")


@pytest.mark.asyncio
async def test_no_intent_id_still_blocks():
    store = _store()
    skill = ChannelPolicyGuard(_policy(auto_execute=False), store)
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"channel": "mystic"})
    result = await skill.run(ctx)
    assert result.status == "skip"
    store.update_policy_state.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_channel_policy_guard.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement ChannelPolicyGuard**

Create `skills/execution/channel_policy_guard.py`:

```python
from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class ChannelPolicyGuard(Skill):
    name = "ChannelPolicyGuard"

    def __init__(self, policy, trade_intent_store) -> None:
        self._policy = policy
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        channel = ctx.get("channel", "")
        intent_id = ctx.get("intent_id")
        channel_cfg = self._policy.watched_channels.get(channel)

        if channel_cfg is None or not channel_cfg.auto_execute:
            reason = f"channel_blocked: channel '{channel}' has auto_execute=False or is not configured"
            logger.info("ChannelPolicyGuard: %s", reason)
            if intent_id:
                await self._store.update_policy_state(intent_id, "channel_blocked")
            return SkillResult(status="skip", reason=reason)

        return SkillResult(status="success")
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_channel_policy_guard.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/channel_policy_guard.py tests/unit/test_channel_policy_guard.py
git commit -m "feat(execution): add ChannelPolicyGuard skill"
```

---

## Task 6: CooldownGuard Skill

**Files:**
- Create: `skills/execution/cooldown_guard.py`
- Create: `tests/unit/test_cooldown_guard.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_cooldown_guard.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.cooldown_guard import CooldownGuard


def _policy(enabled: bool = True, cooldown_minutes: int = 30):
    p = MagicMock()
    p.cooldown_policy.enabled = enabled
    p.cooldown_policy.cooldown_minutes = cooldown_minutes
    return p


def _store(filled_rows=None):
    s = MagicMock()
    s.get_filled_since = AsyncMock(return_value=filled_rows or [])
    s.update_policy_state = AsyncMock()
    return s


def _ctx(ticker="NVDA", intent_id="evt1:NVDA:long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"ticker": ticker, "intent_id": intent_id})
    return ctx


@pytest.mark.asyncio
async def test_no_recent_fill_passes():
    store = _store(filled_rows=[])
    skill = CooldownGuard(_policy(), store)
    result = await skill.run(_ctx())
    assert result.status == "success"


@pytest.mark.asyncio
async def test_recent_fill_blocks():
    store = _store(filled_rows=[{"ticker": "NVDA", "filled_at": "2026-04-24T10:00:00+00:00"}])
    skill = CooldownGuard(_policy(), store)
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "cooldown_blocked" in result.reason
    store.update_policy_state.assert_called_once_with("evt1:NVDA:long", "cooldown_blocked")


@pytest.mark.asyncio
async def test_disabled_policy_always_passes():
    store = _store(filled_rows=[{"ticker": "NVDA", "filled_at": "2026-04-24T10:00:00+00:00"}])
    skill = CooldownGuard(_policy(enabled=False), store)
    result = await skill.run(_ctx())
    assert result.status == "success"
    store.get_filled_since.assert_not_called()


@pytest.mark.asyncio
async def test_different_ticker_not_affected():
    store = _store(filled_rows=[])
    skill = CooldownGuard(_policy(), store)
    result = await skill.run(_ctx(ticker="AAPL"))
    assert result.status == "success"
    # Verify the store was called with AAPL, not NVDA
    call_ticker = store.get_filled_since.call_args[0][0]
    assert call_ticker == "AAPL"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_cooldown_guard.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement CooldownGuard**

Create `skills/execution/cooldown_guard.py`:

```python
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class CooldownGuard(Skill):
    name = "CooldownGuard"

    def __init__(self, policy, trade_intent_store) -> None:
        self._policy = policy
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        cp = self._policy.cooldown_policy
        if not cp.enabled:
            return SkillResult(status="success")

        ticker = ctx.get("ticker", "")
        intent_id = ctx.get("intent_id")
        since = (
            datetime.now(timezone.utc) - timedelta(minutes=cp.cooldown_minutes)
        ).isoformat()

        recent_fills = await self._store.get_filled_since(ticker, since)
        if recent_fills:
            reason = (
                f"cooldown_blocked: filled {ticker} within last {cp.cooldown_minutes}m"
            )
            logger.info("CooldownGuard: %s", reason)
            if intent_id:
                await self._store.update_policy_state(intent_id, "cooldown_blocked")
            return SkillResult(status="skip", reason=reason)

        return SkillResult(status="success")
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_cooldown_guard.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/cooldown_guard.py tests/unit/test_cooldown_guard.py
git commit -m "feat(execution): add CooldownGuard skill"
```

---

## Task 7: ExecutionAuditWriter — add outbox update method

**Files:**
- Modify: `skills/execution/execution_audit_writer.py`
- Modify: `tests/unit/test_execution_audit_writer.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_execution_audit_writer.py`:

```python
@pytest.mark.asyncio
async def test_update_intent_outbox_status(db):
    from infra.storage.trade_intent_store import TradeIntentStore
    from skills.execution.trade_intent_writer import TradeIntentWriter
    from skills.execution.execution_audit_writer import ExecutionAuditWriter
    from datetime import datetime, timezone

    store = TradeIntentStore(db)
    now = datetime.now(timezone.utc).isoformat()
    await store.insert({
        "intent_id": "evt1:NVDA:long",
        "event_id": "evt1",
        "channel": "mystic",
        "ticker": "NVDA",
        "side": "long",
        "instrument_type": "option",
        "conviction": "high",
        "policy_state": "approved",
        "signal_received_at": now,
        "intent_created_at": now,
        "created_at": now,
        "updated_at": now,
    })

    writer = ExecutionAuditWriter(db)
    await writer.update_intent_outbox_status("evt1:NVDA:long", "pending")

    row = await store.get("evt1:NVDA:long")
    assert row["outbox_status"] == "pending"
```

- [ ] **Step 2: Run failing test**

```
pytest tests/unit/test_execution_audit_writer.py::test_update_intent_outbox_status -v
```

Expected: FAIL — method not found.

- [ ] **Step 3: Add method to ExecutionAuditWriter**

In `skills/execution/execution_audit_writer.py`, add after the existing `write` method:

```python
    async def update_intent_outbox_status(self, intent_id: str, outbox_status: str) -> None:
        from datetime import datetime, timezone
        await self._conn.execute(
            "UPDATE trade_intents SET outbox_status=?, updated_at=? WHERE intent_id=?",
            (outbox_status, datetime.now(timezone.utc).isoformat(), intent_id),
        )
        await self._conn.commit()
        logger.debug("ExecutionAuditWriter: intent %s outbox_status=%s", intent_id, outbox_status)
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/unit/test_execution_audit_writer.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/execution_audit_writer.py tests/unit/test_execution_audit_writer.py
git commit -m "feat(execution): add update_intent_outbox_status to ExecutionAuditWriter"
```

---

## Task 8: ExecutionReconciler — scan outbox-stuck intents

**Files:**
- Modify: `skills/execution/execution_reconciler.py`
- Modify: `tests/unit/test_execution_reconciler.py` (if it exists, else create)

- [ ] **Step 1: Write failing test**

Check if `tests/unit/test_execution_reconciler.py` exists. If not, create it. Add:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.execution_reconciler import ExecutionReconciler


def _reconciler(pending_rows=None, uncertain_rows=None, open_orders=None):
    gateway = MagicMock()
    gateway.get_open_orders = AsyncMock(return_value=open_orders or [])

    exec_store = MagicMock()
    exec_store.get_uncertain_executions = AsyncMock(return_value=uncertain_rows or [])

    intent_store = MagicMock()
    intent_store.get_pending_outbox = AsyncMock(return_value=pending_rows or [])

    return ExecutionReconciler(gateway, exec_store, intent_store, interval_seconds=60)


@pytest.mark.asyncio
async def test_reconcile_scans_pending_outbox():
    row = MagicMock()
    row.__getitem__ = lambda self, key: "evt1:NVDA:long" if key == "intent_id" else "NVDA"
    reconciler = _reconciler(pending_rows=[row])
    await reconciler._reconcile()
    reconciler._intent_store.get_pending_outbox.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_no_pending_no_error():
    reconciler = _reconciler()
    await reconciler._reconcile()
    reconciler._intent_store.get_pending_outbox.assert_called_once()
```

- [ ] **Step 2: Run failing tests**

```
pytest tests/unit/test_execution_reconciler.py -v
```

Expected: FAIL — constructor signature mismatch.

- [ ] **Step 3: Update ExecutionReconciler to accept and scan intent_store**

Replace `skills/execution/execution_reconciler.py`:

```python
from __future__ import annotations
import asyncio
import logging
from infra.ib.models import FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class ExecutionReconciler:
    def __init__(self, gateway, execution_store, trade_intent_store=None,
                 interval_seconds: int = 60) -> None:
        self._gateway = gateway
        self._store = execution_store
        self._intent_store = trade_intent_store
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._loop())
        logger.info("ExecutionReconciler started (interval=%ds)", self._interval)

    async def _loop(self) -> None:
        while True:
            try:
                await self._reconcile()
            except Exception as exc:
                logger.exception("ExecutionReconciler error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _reconcile(self) -> None:
        await self._reconcile_executions()
        await self._reconcile_intents()

    async def _reconcile_executions(self) -> None:
        rows = await self._store.get_uncertain_executions()
        if not rows:
            return
        logger.info("ExecutionReconciler: %d uncertain executions", len(rows))
        try:
            open_orders = await self._gateway.get_open_orders()
        except IBGatewayUnavailable:
            logger.warning("ExecutionReconciler: gateway unavailable, skipping this cycle")
            return
        open_order_ids = {str(o.orderId) for o in open_orders}
        for row in rows:
            broker_order_id = row["broker_order_id"]
            if not broker_order_id:
                continue
            if broker_order_id not in open_order_ids:
                logger.warning(
                    "ExecutionReconciler: order %s not in open orders — manual review needed",
                    broker_order_id,
                )

    async def _reconcile_intents(self) -> None:
        if self._intent_store is None:
            return
        rows = await self._intent_store.get_pending_outbox()
        if not rows:
            return
        logger.warning(
            "ExecutionReconciler: %d intent(s) stuck in pending/dispatched outbox",
            len(rows),
        )
        for row in rows:
            logger.warning(
                "ExecutionReconciler: intent_id=%s outbox_status=%s — manual review needed",
                row["intent_id"],
                row["outbox_status"],
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_execution_reconciler.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/execution_reconciler.py tests/unit/test_execution_reconciler.py
git commit -m "feat(execution): ExecutionReconciler scans outbox-stuck trade_intents"
```

---

## Task 9: Registry + main.py Wiring

**Files:**
- Modify: `agent/registry.py`
- Modify: `main.py`

- [ ] **Step 1: Update registry.py**

In `agent/registry.py`, update `build_phase2b_execution_chain` to accept and wire the new skills:

```python
def build_phase2b_execution_chain(policy, execution_store, gateway, trade_intent_store=None) -> list:
    from skills.execution.trade_intent_writer import TradeIntentWriter
    from skills.execution.channel_policy_guard import ChannelPolicyGuard
    from skills.execution.cooldown_guard import CooldownGuard
    from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
    from skills.execution.chain_lookup import ChainLookup
    from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
    from skills.execution.contract_selector import ContractSelector
    from skills.execution.order_sizer import OrderSizer
    from skills.execution.order_pricer import OrderPricer
    from skills.execution.order_submitter import OrderSubmitter
    from skills.execution.fill_waiter import FillWaiter

    guards = []
    if trade_intent_store is not None:
        guards = [
            TradeIntentWriter(trade_intent_store),
            ChannelPolicyGuard(policy, trade_intent_store),
            CooldownGuard(policy, trade_intent_store),
        ]

    return guards + [
        ExecutionEligibilityGuard(policy),
        ChainLookup(gateway, execution_store._conn),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store,
                   timeout=policy.execution.fill_wait_timeout_seconds),
    ]
```

- [ ] **Step 2: Update main.py**

In `main.py`, import and instantiate `TradeIntentStore` and pass it to the chains:

After `execution_store = ExecutionStore(conn)`, add:

```python
    from infra.storage.trade_intent_store import TradeIntentStore
    trade_intent_store = TradeIntentStore(conn)
```

Update the `build_phase2b_execution_chain` call:

```python
    phase2b_chain = build_phase2b_execution_chain(
        policy, execution_store, gateway, trade_intent_store
    )
```

Update the `ExecutionReconciler` instantiation:

```python
    reconciler = ExecutionReconciler(
        gateway, execution_store, trade_intent_store,
        interval_seconds=policy.execution.reconciler_interval_seconds,
    )
```

- [ ] **Step 3: Run existing e2e test to verify no regressions**

```
pytest tests/e2e/test_phase2b_execution_pipeline.py -v
```

Expected: all PASS (the intent store guard path is only active when `trade_intent_store` is provided, and the db fixture has the new schema from Task 1).

- [ ] **Step 4: Commit**

```bash
git add agent/registry.py main.py
git commit -m "feat(registry): wire TradeIntentWriter, ChannelPolicyGuard, CooldownGuard into phase2b"
```

---

## Task 10: E2E Test — intent row creation + policy guard path

**Files:**
- Modify: `tests/e2e/test_phase2b_execution_pipeline.py`

- [ ] **Step 1: Add e2e tests covering intent row and policy blocking**

Add to `tests/e2e/test_phase2b_execution_pipeline.py`:

```python
from infra.storage.trade_intent_store import TradeIntentStore
from skills.execution.trade_intent_writer import TradeIntentWriter
from skills.execution.channel_policy_guard import ChannelPolicyGuard
from skills.execution.cooldown_guard import CooldownGuard


def _policy_with_intent(auto_execute: bool = True):
    p = _policy()
    ch_cfg = MagicMock()
    ch_cfg.auto_execute = auto_execute
    p.watched_channels = {"mystic": ch_cfg}
    return p


@pytest.mark.asyncio
async def test_intent_row_created_on_happy_path(db):
    policy = _policy_with_intent(auto_execute=True)
    gateway = _gateway()
    execution_store = ExecutionStore(db)
    intent_store = TradeIntentStore(db)
    trace_store = TraceStore(db)

    chain = [
        TradeIntentWriter(intent_store),
        ChannelPolicyGuard(policy, intent_store),
        CooldownGuard(policy, intent_store),
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id=str(uuid.uuid4())[:12], event_id="evt-intent-1")
    ctx.update({
        "signal_id": "sig-1",
        "ticker": "AAPL",
        "conviction_bucket": "high",
        "spot_price": 152.0,
        "channel": "mystic",
        "intent": "LONG_SIGNAL",
        "received_at": "2026-04-24T14:00:00+00:00",
    })

    await orch.run(ctx)

    intent_id = "evt-intent-1:AAPL:long"
    row = await intent_store.get(intent_id)
    assert row is not None
    assert row["ticker"] == "AAPL"
    assert row["policy_state"] == "approved"


@pytest.mark.asyncio
async def test_channel_blocked_skips_execution(db):
    policy = _policy_with_intent(auto_execute=False)
    gateway = _gateway()
    execution_store = ExecutionStore(db)
    intent_store = TradeIntentStore(db)
    trace_store = TraceStore(db)

    chain = [
        TradeIntentWriter(intent_store),
        ChannelPolicyGuard(policy, intent_store),
        CooldownGuard(policy, intent_store),
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id=str(uuid.uuid4())[:12], event_id="evt-blocked-1")
    ctx.update({
        "signal_id": "sig-2",
        "ticker": "AAPL",
        "conviction_bucket": "high",
        "spot_price": 152.0,
        "channel": "mystic",
        "intent": "LONG_SIGNAL",
        "received_at": "2026-04-24T14:00:00+00:00",
    })

    await orch.run(ctx)

    intent_id = "evt-blocked-1:AAPL:long"
    row = await intent_store.get(intent_id)
    assert row is not None
    assert row["policy_state"] == "channel_blocked"

    # No execution row should exist
    async with db.execute("SELECT count(*) as n FROM executions") as cur:
        count_row = await cur.fetchone()
    assert count_row["n"] == 0
```

- [ ] **Step 2: Run e2e tests**

```
pytest tests/e2e/test_phase2b_execution_pipeline.py -v
```

Expected: all PASS including new tests.

- [ ] **Step 3: Run full test suite**

```
pytest --tb=short -q
```

Expected: all PASS with no regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_phase2b_execution_pipeline.py
git commit -m "test(e2e): assert trade_intent row creation and channel policy blocking"
```

---

## Spec 1 Complete

All tasks done. The `trade_intents` table is the durable backbone for every signal. `ChannelPolicyGuard` and `CooldownGuard` enforce terminal policy states before execution. `ExecutionReconciler` watches for outbox-stuck intents. Spec 2 builds on this schema to add `SignalAnalyzer`, `PriceWalker`, and the fast chain lookup.
