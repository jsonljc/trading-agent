# Shares-First + Options Sleeve, Per-Channel Sizing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current option-or-LMT-walk entry path with shares-first MKT, plus a 5%-of-base options-sleeve gated by a 10% chase guard against the reference price; size off `NetLiquidation × margin_multiplier` via a per-channel sizing table; arm the trim ladder on shares only; correct the swapped mystic ↔ stock-talk-portfolio channel IDs; add `urkel` + `pup-danny` channels and trader profiles; fix the WSE 3%-but-classified-HIGH bug.

**Architecture:** Each Phase 2b signal flows through one execution chain that runs the shares sub-chain to completion, then conditionally runs the options sub-chain gated by `OptionsChaseGuard`. Sizing is a config-driven lookup keyed on `(channel, bucket)`, with a default row for new channels. State for the (carryover) trim ladder lives in a new `trade_intent_trims` table; options orders link to their parent shares order via a new `parent_intent_id` column on `trade_intents`. The 2026-05-04 trim-ladder spec was never executed, so its scaffolding (schema, gateway MKT support, RthEntryGuard, EquityContractBuilder, SharesMarketSubmitter, ExitLadder) folds into this plan.

**Tech Stack:** Python 3 + asyncio, ib_insync (IB Gateway), aiosqlite (state), pydantic (config), pytest + pytest-asyncio (tests). Paper account only — `_assert_paper_guard` in `infra/ib/gateway.py:332` stays in force.

**Spec:** `docs/superpowers/specs/2026-05-05-shares-plus-options-sizing-design.md` (supersedes the 2026-05-04 spec).

---

## File Structure

**New files:**
- `skills/execution/rth_entry_guard.py` — drops non-RTH entries (carryover from 2026-05-04)
- `skills/execution/equity_contract_builder.py` — qualifies STK contract (carryover)
- `skills/execution/reference_price_capture.py` — snapshots quote into `ctx["reference_price"]`
- `skills/execution/sizing_resolver.py` — looks up `(channel, bucket)` in policy, sets `ctx["shares_pct"]` and `ctx["options_pct"]`
- `skills/execution/shares_market_submitter.py` — MKT BUY shares, persist fill, arm trims (carryover, modified to write `parent_intent_id=null`)
- `skills/execution/options_chase_guard.py` — re-quote, skip options if current > ref × 1.10
- `skills/execution/options_market_submitter.py` — MKT BUY options, write second `trade_intents` row with `parent_intent_id` set
- `infra/storage/trim_ladder_store.py` — CRUD for `trade_intent_trims` (carryover)
- `agent/exit_ladder.py` — background poll/fire loop (carryover)
- `config/traders/urkel.yaml`
- `config/traders/pup-danny.yaml`
- `tests/unit/test_schema_v2026_05_05.py`
- `tests/unit/test_trim_ladder_store.py`
- `tests/unit/test_trader_classifier_wse_fix.py`
- `tests/unit/test_gateway_market_order.py`
- `tests/unit/test_rth_entry_guard.py`
- `tests/unit/test_equity_contract_builder.py`
- `tests/unit/test_reference_price_capture.py`
- `tests/unit/test_sizing_resolver.py`
- `tests/unit/test_order_sizer_netliq.py`
- `tests/unit/test_shares_market_submitter.py`
- `tests/unit/test_options_chase_guard.py`
- `tests/unit/test_options_market_submitter.py`
- `tests/unit/test_exit_ladder.py`
- `tests/unit/test_policy_sizing_schema.py`
- `tests/unit/test_trader_registry_new_profiles.py`

**Modified files:**
- `infra/storage/db.py` — add `fill_qty`, `parent_intent_id` to `trade_intents`; add `trade_intent_trims` table
- `infra/storage/trade_intent_store.py` — new CRUD methods accept/return `fill_qty` and `parent_intent_id`
- `agent/policy.py` — `ExecutionPolicy` adds `margin_multiplier`, `sizing`, `exit_poll_interval_seconds`, `trim_ladder`, `options_chase_threshold_pct`; `InstrumentPolicy` drops `fallback_to_stock_if_no_options` (or marks ignored)
- `config/policy.yaml` — channel-ID swap, sizing table, new keys, new channel mappings
- `infra/ib/models.py` — `PreparedOrder.limit_price: float | None` (None for MKT)
- `infra/ib/gateway.py` — `place_order` branches on `order.order_type` ∈ {`"LMT"`, `"MKT"`}
- `skills/signal/trader_classifier.py` — bucket-only shortcut, WSE small-size override, drop `SIZE_LOW`/`SIZE_HIGH`/`MAX_STATED_SIZE`/`SIZE_HIGH_SHORTCUT_THRESHOLD` constants from this skill
- `skills/execution/order_sizer.py` — `NetLiquidation × margin_multiplier × pct`, reads `shares_pct` or `options_pct` based on `instrument_type`
- `skills/execution/trade_intent_writer.py` — write `instrument_type` from ctx (not hardcoded), accept and persist `parent_intent_id`
- `agent/registry.py` — `build_phase2b_execution_chain` rebuilt around the new sub-chains
- `main.py` — start the `ExitLadder` background task
- `tests/unit/test_trader_classifier.py` — update expected sizes (no longer constants)
- `tests/unit/test_order_sizer.py` — update for NetLiq base + dual-key reads
- `tests/unit/test_trade_intent_writer.py` — assert dynamic `instrument_type` and `parent_intent_id`

**Bypassed (kept in repo, removed from entry chain):** `chain_lookup.py`, `instrument_marketability_guard.py`, `contract_selector.py` are still used by the **options sub-chain** (gated by `OptionsChaseGuard`). `order_pricer.py` and `price_walker.py` are bypassed entirely (both legs are MKT now). Their tests stay green; we just don't wire them into the entry chain.

---

## Task 1: Schema additions (`fill_qty`, `parent_intent_id`, `trade_intent_trims`)

**Files:**
- Modify: `infra/storage/db.py`
- Test: `tests/unit/test_schema_v2026_05_05.py` (new)

- [ ] **Step 1.1: Write the failing schema test**

Create `tests/unit/test_schema_v2026_05_05.py`:

```python
import pytest
import aiosqlite
from infra.storage.db import SCHEMA


@pytest.mark.asyncio
async def test_trade_intents_has_fill_qty_and_parent_intent_id():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(trade_intents)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "fill_qty" in cols
        assert "parent_intent_id" in cols


@pytest.mark.asyncio
async def test_trade_intent_trims_table_exists():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(trade_intent_trims)") as cur:
            cols = {row["name"]: row for row in await cur.fetchall()}
        for col in ("intent_id", "rung", "threshold_pct", "trim_pct",
                    "armed_at", "fired_at", "fire_price",
                    "sold_qty", "sold_avg_price", "broker_order_ref"):
            assert col in cols, f"trade_intent_trims missing {col}"
```

- [ ] **Step 1.2: Run, verify failure**

`pytest tests/unit/test_schema_v2026_05_05.py -v` → both fail (`fill_qty` not in cols, table missing).

- [ ] **Step 1.3: Add columns + table to `SCHEMA` in `infra/storage/db.py`**

Append to the `trade_intents` `CREATE TABLE` (just before the closing paren) two new columns:

```sql
    fill_qty               INTEGER,
    parent_intent_id       TEXT,
```

Append after the `trade_intents` block:

```sql
CREATE TABLE IF NOT EXISTS trade_intent_trims (
    intent_id            TEXT NOT NULL,
    rung                 INTEGER NOT NULL,
    threshold_pct        REAL NOT NULL,
    trim_pct             REAL NOT NULL,
    armed_at             TEXT NOT NULL,
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
```

- [ ] **Step 1.4: Run, verify pass**

`pytest tests/unit/test_schema_v2026_05_05.py -v` → both pass.

- [ ] **Step 1.5: Commit**

```bash
git add infra/storage/db.py tests/unit/test_schema_v2026_05_05.py
git commit -m "schema: add fill_qty, parent_intent_id, trade_intent_trims"
```

---

## Task 2: TrimLadderStore (CRUD)

**Files:**
- Create: `infra/storage/trim_ladder_store.py`
- Test: `tests/unit/test_trim_ladder_store.py`

- [ ] **Step 2.1: Write failing tests**

```python
import pytest
import aiosqlite
from infra.storage.db import SCHEMA
from infra.storage.trim_ladder_store import TrimLadderStore


@pytest.fixture
async def store():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    yield TrimLadderStore(conn)
    await conn.close()


@pytest.mark.asyncio
async def test_arm_inserts_two_rungs(store):
    await store.arm("intent-1", rungs=[(1, 0.05, 0.40), (2, 0.10, 0.40)],
                    armed_at="2026-05-05T10:00:00Z")
    rows = await store.unfired_for_intent("intent-1")
    assert len(rows) == 2
    assert {r["rung"] for r in rows} == {1, 2}


@pytest.mark.asyncio
async def test_record_fire_marks_rung_fired(store):
    await store.arm("intent-1", rungs=[(1, 0.05, 0.40)], armed_at="2026-05-05T10:00:00Z")
    await store.record_fire(
        intent_id="intent-1", rung=1,
        fired_at="2026-05-05T10:30:00Z",
        fire_price=110.0, sold_qty=4, sold_avg_price=110.05,
        broker_order_ref="order-99",
    )
    rows = await store.unfired_for_intent("intent-1")
    assert rows == []
    fired = await store.all_for_intent("intent-1")
    assert fired[0]["fired_at"] == "2026-05-05T10:30:00Z"
    assert fired[0]["sold_qty"] == 4


@pytest.mark.asyncio
async def test_unfired_across_intents(store):
    await store.arm("intent-1", rungs=[(1, 0.05, 0.40)], armed_at="t1")
    await store.arm("intent-2", rungs=[(1, 0.05, 0.40), (2, 0.10, 0.40)], armed_at="t2")
    rows = await store.all_unfired()
    intent_ids = {r["intent_id"] for r in rows}
    assert intent_ids == {"intent-1", "intent-2"}
    assert len(rows) == 3
```

- [ ] **Step 2.2: Run, verify failure (module missing)**

- [ ] **Step 2.3: Implement the store**

```python
# infra/storage/trim_ladder_store.py
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
        async with self._conn.execute(
            "SELECT * FROM trade_intent_trims WHERE fired_at IS NULL "
            "ORDER BY intent_id, rung",
        ) as cur:
            return list(await cur.fetchall())
```

- [ ] **Step 2.4: Run, verify pass**

- [ ] **Step 2.5: Commit**

```bash
git add infra/storage/trim_ladder_store.py tests/unit/test_trim_ladder_store.py
git commit -m "store: TrimLadderStore CRUD on trade_intent_trims"
```

---

## Task 3: Pydantic policy schema additions

**Files:**
- Modify: `agent/policy.py`
- Test: `tests/unit/test_policy_sizing_schema.py`

- [ ] **Step 3.1: Write failing tests**

```python
import pytest
import yaml
from agent.policy import PolicyModel


def _base_policy_dict() -> dict:
    return {
        "trigger": {"action_words": ["long"]},
        "instrument_policy": {
            "prefer_options": True, "min_expiry_days": 180,
            "strike_policy": "closest_itm_call",
        },
        "pricing_policy": {"mode": "cheapest_fillable_limit",
                           "option_spread_fraction": 0.25, "stock_buffer_pct": 0.001},
        "market_hours": {"options_rth_only": True, "stock_premarket_allowed": True,
                         "stock_premarket_start": "04:00", "rth_start": "09:30",
                         "rth_end": "16:00", "stock_afterhours_queue": True},
        "cooldown_policy": {"enabled": True, "cooldown_minutes": 30},
        "dedupe_policy": {"enabled": True, "key": "x"},
        "pricing_policy_guards": {"min_bid": 0.01, "max_spread_pct": 0.40},
        "models": {"vision": "claude-opus-4-7", "text": "claude-haiku-4-5"},
        "watched_channels": {"mystic": {"auto_execute": True}},
        "discord_bundle_id": "x",
        "telegram": {"chat_id": "1", "bot_token": "x"},
        "execution": {
            "margin_multiplier": 2.0,
            "options_chase_threshold_pct": 0.10,
            "exit_poll_interval_seconds": 2,
            "trim_ladder": {"rungs": [
                {"threshold_pct": 0.05, "trim_pct": 0.40},
                {"threshold_pct": 0.10, "trim_pct": 0.40},
            ]},
            "sizing": {
                "default": {
                    "high": {"shares": 0.10, "options": 0.05},
                    "low":  {"shares": 0.05, "options": 0.05},
                },
                "per_channel": {
                    "stock-talk-portfolio": {
                        "high": {"shares": 0.20, "options": 0.05},
                        "low":  {"shares": 0.15, "options": 0.05},
                    },
                    "mystic": {
                        "high": {"shares": 0.15, "options": 0.05},
                        "low":  {"shares": 0.10, "options": 0.05},
                    },
                },
            },
        },
    }


def test_loads_full_sizing_table():
    pol = PolicyModel.model_validate(_base_policy_dict())
    assert pol.execution.margin_multiplier == 2.0
    assert pol.execution.options_chase_threshold_pct == 0.10
    assert pol.execution.exit_poll_interval_seconds == 2
    assert pol.execution.trim_ladder.rungs[0].threshold_pct == 0.05
    assert pol.execution.sizing.default.high.shares == 0.10
    assert pol.execution.sizing.per_channel["stock-talk-portfolio"].high.shares == 0.20


def test_rejects_out_of_range_pct():
    bad = _base_policy_dict()
    bad["execution"]["sizing"]["default"]["high"]["shares"] = 2.0
    with pytest.raises(Exception):
        PolicyModel.model_validate(bad)


def test_default_margin_multiplier_when_omitted():
    d = _base_policy_dict()
    d["execution"].pop("margin_multiplier")
    pol = PolicyModel.model_validate(d)
    assert pol.execution.margin_multiplier == 2.0
```

- [ ] **Step 3.2: Run, verify failure**

- [ ] **Step 3.3: Implement schema additions in `agent/policy.py`**

Replace `InstrumentPolicy` to drop `fallback_to_stock_if_no_options`:

```python
class InstrumentPolicy(BaseModel):
    prefer_options: bool
    min_expiry_days: int
    strike_policy: str
    # fallback_to_stock_if_no_options removed (dead under shares-first design)
```

Add new models above `ExecutionPolicy`:

```python
class SizingTier(BaseModel):
    shares: float = Field(ge=0.0, le=1.0)
    options: float = Field(ge=0.0, le=1.0)


class SizingBuckets(BaseModel):
    high: SizingTier
    low: SizingTier


class SizingPolicy(BaseModel):
    default: SizingBuckets
    per_channel: dict[str, SizingBuckets] = Field(default_factory=dict)


class TrimRung(BaseModel):
    threshold_pct: float = Field(ge=0.0, le=1.0)
    trim_pct: float = Field(ge=0.0, le=1.0)


class TrimLadderConfig(BaseModel):
    rungs: list[TrimRung]
```

Replace `ExecutionPolicy` with:

```python
class ExecutionPolicy(BaseModel):
    fill_wait_timeout_seconds: float = 30.0
    max_equity_price: float = 500.0
    reconciler_interval_seconds: int = 60
    walk_profile: str = "aggressive_fast"
    walk_profiles: dict[str, list[float]] = {
        "cautious_fast":   [0.00, 0.02, 0.05, 0.10],
        "aggressive_fast": [0.01, 0.03, 0.06, 0.10],
    }
    reprice_interval_ms: int = 2500
    max_chase_pct: float = 0.15
    margin_multiplier: float = 2.0
    options_chase_threshold_pct: float = 0.10
    exit_poll_interval_seconds: int = 2
    trim_ladder: TrimLadderConfig = TrimLadderConfig(rungs=[
        TrimRung(threshold_pct=0.05, trim_pct=0.40),
        TrimRung(threshold_pct=0.10, trim_pct=0.40),
    ])
    sizing: SizingPolicy = SizingPolicy(
        default=SizingBuckets(
            high=SizingTier(shares=0.10, options=0.05),
            low=SizingTier(shares=0.05, options=0.05),
        ),
    )
```

- [ ] **Step 3.4: Run, verify pass**

- [ ] **Step 3.5: Commit**

```bash
git add agent/policy.py tests/unit/test_policy_sizing_schema.py
git commit -m "policy: add margin_multiplier, sizing, trim_ladder, options_chase config"
```

---

## Task 4: `policy.yaml` updates (channel swap, sizing, drop dead keys)

**Files:**
- Modify: `config/policy.yaml`
- Test: `tests/unit/test_policy_yaml_loads.py` (new)

- [ ] **Step 4.1: Write failing test that loads the live YAML**

```python
import pytest
from agent.policy import load_policy


def test_live_policy_yaml_loads():
    pol = load_policy("config/policy.yaml")
    cm = pol.discord_extension.channel_id_map
    assert cm["1229546005788098580"] == "stocktalkweekly"
    assert cm["1217309136681832540"] == "mystic"
    assert cm["1248378121451733083"] == "wallstengine"
    assert cm["1221605346305642558"] == "pup-danny"
    assert cm["1151611275709788253"] == "urkel"
    s = pol.execution.sizing
    assert s.per_channel["stock-talk-portfolio"].high.shares == 0.20
    assert s.per_channel["mystic"].low.shares == 0.10
    assert s.default.high.shares == 0.10
    assert pol.execution.margin_multiplier == 2.0
    assert pol.execution.options_chase_threshold_pct == 0.10
```

- [ ] **Step 4.2: Run, verify failure**

- [ ] **Step 4.3: Edit `config/policy.yaml`**

Replace the `instrument_policy` block (drop `fallback_to_stock_if_no_options` line):

```yaml
instrument_policy:
  prefer_options: true
  min_expiry_days: 180
  strike_policy: closest_itm_call
```

Add to the `execution:` block (preserve existing keys, append):

```yaml
  margin_multiplier: 2.0
  options_chase_threshold_pct: 0.10
  exit_poll_interval_seconds: 2
  trim_ladder:
    rungs:
      - threshold_pct: 0.05
        trim_pct: 0.40
      - threshold_pct: 0.10
        trim_pct: 0.40
  sizing:
    default:
      high: { shares: 0.10, options: 0.05 }
      low:  { shares: 0.05, options: 0.05 }
    per_channel:
      stock-talk-portfolio:
        high: { shares: 0.20, options: 0.05 }
        low:  { shares: 0.15, options: 0.05 }
      mystic:
        high: { shares: 0.15, options: 0.05 }
        low:  { shares: 0.10, options: 0.05 }
```

Replace the `discord_extension.channel_id_map` block:

```yaml
discord_extension:
  forwarder_port: 9876
  channel_id_map:
    "1229546005788098580": stocktalkweekly
    "1217309136681832540": mystic
    "1248378121451733083": wallstengine
    "1221605346305642558": pup-danny
    "1151611275709788253": urkel
```

- [ ] **Step 4.4: Run, verify pass**

- [ ] **Step 4.5: Commit**

```bash
git add config/policy.yaml tests/unit/test_policy_yaml_loads.py
git commit -m "config: swap mystic/STP channel IDs, add urkel+pup-danny, sizing table"
```

---

## Task 5: New trader profile YAMLs (urkel, pup-danny)

**Files:**
- Create: `config/traders/urkel.yaml`, `config/traders/pup-danny.yaml`
- Test: `tests/unit/test_trader_registry_new_profiles.py`

- [ ] **Step 5.1: Write failing test**

```python
from agent.traders.registry import TraderRegistry


def test_registry_loads_urkel_and_pup_danny():
    reg = TraderRegistry.from_dir("config/traders")
    handles = {p.handle for p in reg.all()}
    assert "urkel" in handles
    assert "pup-danny" in handles
    urkel = next(p for p in reg.all() if p.handle == "urkel")
    assert urkel.auto_execute is True
    assert urkel.conviction_examples == ()
```

- [ ] **Step 5.2: Run, verify failure (handles missing)**

- [ ] **Step 5.3: Create `config/traders/urkel.yaml`**

```yaml
handle: urkel
display_name: Urkel
discord_author_pattern: "Urkel"
alert_mention: ""
require_alert_mention: false
bot_authors_to_skip: []
auto_execute: true
size_in_message: false
prefer_message_size: false
classifier_model: claude-haiku-4-5
availability_phrases: []
conviction_examples: []
```

- [ ] **Step 5.4: Create `config/traders/pup-danny.yaml`**

```yaml
handle: pup-danny
display_name: Pup Danny
discord_author_pattern: "Pup Danny"
alert_mention: ""
require_alert_mention: false
bot_authors_to_skip: []
auto_execute: true
size_in_message: false
prefer_message_size: false
classifier_model: claude-haiku-4-5
availability_phrases: []
conviction_examples: []
```

- [ ] **Step 5.5: Run, verify pass**

- [ ] **Step 5.6: Commit**

```bash
git add config/traders/urkel.yaml config/traders/pup-danny.yaml \
    tests/unit/test_trader_registry_new_profiles.py
git commit -m "traders: add urkel + pup-danny stub profiles"
```

---

## Task 6: TraderClassifier — bucket-only shortcut + WSE small-size override

**Files:**
- Modify: `skills/signal/trader_classifier.py`
- Modify: `tests/unit/test_trader_classifier.py`
- Test: `tests/unit/test_trader_classifier_wse_fix.py` (new)

- [ ] **Step 6.1: Write failing tests for the new behavior**

`tests/unit/test_trader_classifier_wse_fix.py`:

```python
import pytest
from unittest.mock import AsyncMock
from skills.signal.trader_classifier import TraderClassifier
from agent.traders.profile import TraderProfile, ConvictionExample
from agent.context import Context

WSE = TraderProfile(
    handle="wallstengine", display_name="WSE",
    discord_author_pattern="WSE", alert_mention="",
    require_alert_mention=False, bot_authors_to_skip=(),
    auto_execute=True, size_in_message=True, prefer_message_size=True,
    classifier_model="x", availability_phrases=(), conviction_examples=(),
)


class _Reg:
    def all(self): return [WSE]


def _llm(bucket="HIGH", confidence=0.85, ticker="CEG", side="long"):
    m = AsyncMock()
    m.classify.return_value = {
        "is_entry": True, "ticker": ticker, "side": side,
        "bucket": bucket, "confidence": confidence, "reason": "thesis",
    }
    return m


@pytest.mark.asyncio
async def test_wse_small_size_overrides_llm_high():
    """3% pos with multi-ticker → LLM path; LLM says HIGH; we force LOW."""
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = Context()
    ctx.update({
        "trader_handle": "wallstengine",
        "full_message_text": "Added 3% pos in $CEG, paired with $VST exposure.",
    })
    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("bucket") == "LOW"
    assert ctx.get("size_source") == "wse_small_size_override"


@pytest.mark.asyncio
async def test_high_stated_size_does_not_trigger_override():
    """10% stated → no override; LLM HIGH stays HIGH."""
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = Context()
    ctx.update({
        "trader_handle": "wallstengine",
        "full_message_text": "Added 10% pos in $CEG, $VST.",
    })
    await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"


@pytest.mark.asyncio
async def test_no_stated_size_no_override():
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = Context()
    ctx.update({
        "trader_handle": "wallstengine",
        "full_message_text": "OPEN $CEG, $VST structured thesis.",
    })
    await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"


@pytest.mark.asyncio
async def test_shortcut_sets_bucket_only_no_size_pct():
    """Shortcut (single ticker + entry verb + stated size) sets bucket; size_pct stays None."""
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = Context()
    ctx.update({
        "trader_handle": "wallstengine",
        "full_message_text": "Added 5% pos in $CEG.",
    })
    await classifier.run(ctx)
    assert ctx.get("bucket") == "LOW"  # 5% < 7.5
    assert ctx.get("size_source") == "shortcut_stated"
    assert ctx.get("size_pct") in (None, 0.0)


@pytest.mark.asyncio
async def test_shortcut_high_for_large_stated_size():
    classifier = TraderClassifier(_Reg(), _llm("HIGH", 0.85))
    ctx = Context()
    ctx.update({
        "trader_handle": "wallstengine",
        "full_message_text": "Added 12% pos in $CEG.",
    })
    await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_source") == "shortcut_stated"
```

- [ ] **Step 6.2: Run, verify failure**

- [ ] **Step 6.3: Edit `skills/signal/trader_classifier.py`**

Replace the constants block (lines 17-20) with a single threshold:

```python
HIGH_CONF_THRESHOLD = 0.80
DROP_CONF_THRESHOLD = 0.50
SMALL_SIZE_THRESHOLD = 7.5  # stated_size_pct < this → force LOW
```

In the shortcut block (lines 67–89), replace size_pct assignment so the shortcut sets bucket only:

```python
        if (
            profile.prefer_message_size
            and features.stated_size_pct is not None
            and features.entry_verb_present
            and len(features.tickers_in_msg) == 1
        ):
            bucket = "HIGH" if features.stated_size_pct >= SMALL_SIZE_THRESHOLD else "LOW"
            updates = {
                "ticker": features.tickers_in_msg[0],
                "side": "long",
                "bucket": bucket,
                "confidence": 1.0,
                "size_source": "shortcut_stated",
                "classifier_features_json": json.dumps(dataclasses.asdict(features)),
                "classifier_llm_response_json": None,
                "classifier_reason": "stated_size_in_message",
            }
            ctx.update(updates)
            return SkillResult(status="success", updates=updates)
```

In the LLM-success branch (currently lines 164-176), apply the WSE small-size override after final_bucket is computed:

```python
        if confidence < HIGH_CONF_THRESHOLD:
            final_bucket = "LOW"
            size_source = "downgrade"
        else:
            final_bucket = bucket
            size_source = "bucket_high" if bucket == "HIGH" else "bucket_low"

        # WSE-fix: explicit small stated size always wins over LLM thesis read
        if (features.stated_size_pct is not None
                and features.stated_size_pct < SMALL_SIZE_THRESHOLD
                and final_bucket == "HIGH"):
            final_bucket = "LOW"
            size_source = "wse_small_size_override"

        updates = {
            "ticker": ticker, "side": side,
            "bucket": final_bucket, "confidence": confidence,
            "size_source": size_source,
            "classifier_features_json": features_json,
            "classifier_llm_response_json": llm_json,
            "classifier_reason": reason,
        }
```

(No `size_pct` key in updates — sizing moves to `SizingResolver` in Task 11.)

- [ ] **Step 6.4: Update `tests/unit/test_trader_classifier.py`**

Where existing tests assert `ctx.get("size_pct") == 0.05` or similar, replace with `assert "size_pct" not in ctx.snapshot()` (or whatever ctx accessor is canonical — check the test file's existing patterns). Where they assert `bucket="HIGH"` and expected sizing, drop the size assertion. The bucket assertions stay.

- [ ] **Step 6.5: Run all classifier tests, verify pass**

`pytest tests/unit/test_trader_classifier.py tests/unit/test_trader_classifier_wse_fix.py -v`

- [ ] **Step 6.6: Commit**

```bash
git add skills/signal/trader_classifier.py tests/unit/test_trader_classifier.py \
    tests/unit/test_trader_classifier_wse_fix.py
git commit -m "classifier: bucket-only shortcut + WSE small-size override"
```

---

## Task 7: PreparedOrder.limit_price optional + Gateway MKT branch

**Files:**
- Modify: `infra/ib/models.py`, `infra/ib/gateway.py`
- Test: `tests/unit/test_gateway_market_order.py`

- [ ] **Step 7.1: Write failing test**

```python
import pytest
from unittest.mock import MagicMock, AsyncMock
from infra.ib.gateway import IBGateway
from infra.ib.models import PreparedOrder, BrokerContractRef


@pytest.fixture
def gw_with_mock_ib():
    gw = IBGateway.__new__(IBGateway)
    gw._ib = MagicMock()
    gw._ib.qualifyContractsAsync = AsyncMock(return_value=[MagicMock()])
    gw._ib.placeOrder = MagicMock(return_value=MagicMock())
    gw._read_breaker = MagicMock()
    gw._write_breaker = MagicMock()
    gw._policy = MagicMock(ib_gateway=MagicMock(mode="paper",
                                                  paper_account_prefixes=["DU"]))
    gw._account_id = "DUQ123"
    return gw


@pytest.mark.asyncio
async def test_place_order_market_uses_market_order(gw_with_mock_ib):
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    order = PreparedOrder(action="BUY", quantity=10, order_type="MKT",
                          limit_price=None, tif="DAY")
    await gw_with_mock_ib.place_order(contract, order, "client-1")
    placed = gw_with_mock_ib._ib.placeOrder.call_args[0][1]
    assert type(placed).__name__ == "MarketOrder"


@pytest.mark.asyncio
async def test_place_order_limit_unchanged(gw_with_mock_ib):
    contract = BrokerContractRef(symbol="AAPL", sec_type="STK",
                                 exchange="SMART", currency="USD", qualified=True)
    order = PreparedOrder(action="BUY", quantity=10, order_type="LMT",
                          limit_price=150.0, tif="DAY")
    await gw_with_mock_ib.place_order(contract, order, "client-2")
    placed = gw_with_mock_ib._ib.placeOrder.call_args[0][1]
    assert type(placed).__name__ == "LimitOrder"
    assert placed.lmtPrice == 150.0
```

- [ ] **Step 7.2: Run, verify failure**

- [ ] **Step 7.3: Make `PreparedOrder.limit_price` optional**

In `infra/ib/models.py`, replace:

```python
@dataclass
class PreparedOrder:
    action: str
    quantity: int
    order_type: str
    limit_price: float | None
    tif: str
```

- [ ] **Step 7.4: Branch `place_order` in `infra/ib/gateway.py`**

Replace lines 336-350 (the `try:` body) with:

```python
        try:
            from ib_insync import LimitOrder, MarketOrder
            ib_contract = _to_ib_contract(contract_ref)
            if order.order_type == "MKT":
                ib_order = MarketOrder(
                    action=order.action,
                    totalQuantity=order.quantity,
                    tif=order.tif,
                    orderRef=client_order_id,
                )
            else:
                if order.limit_price is None:
                    raise ValueError(f"LMT order requires limit_price; got None")
                ib_order = LimitOrder(
                    action=order.action,
                    totalQuantity=order.quantity,
                    lmtPrice=order.limit_price,
                    tif=order.tif,
                    orderRef=client_order_id,
                )
            trade = self._ib.placeOrder(ib_contract, ib_order)
            self._write_breaker._record_success()
            logger.info("Placed %s %s: %s x%s",
                        order.order_type, client_order_id,
                        contract_ref.symbol, order.quantity)
            return trade
```

- [ ] **Step 7.5: Run, verify pass**

- [ ] **Step 7.6: Commit**

```bash
git add infra/ib/models.py infra/ib/gateway.py tests/unit/test_gateway_market_order.py
git commit -m "gateway: support MKT branch in place_order; PreparedOrder.limit_price optional"
```

---

## Task 8: RthEntryGuard

**Files:**
- Create: `skills/execution/rth_entry_guard.py`
- Test: `tests/unit/test_rth_entry_guard.py`

- [ ] **Step 8.1: Write failing tests**

```python
import pytest
from skills.execution.rth_entry_guard import RthEntryGuard
from agent.context import Context


@pytest.mark.asyncio
async def test_passes_during_rth():
    g = RthEntryGuard()
    ctx = Context()
    ctx.update({"execution_session": "rth"})
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_skips_premarket():
    g = RthEntryGuard()
    ctx = Context()
    ctx.update({"execution_session": "premarket"})
    result = await g.run(ctx)
    assert result.status == "skip"
    assert "rth" in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_skips_afterhours():
    g = RthEntryGuard()
    ctx = Context()
    ctx.update({"execution_session": "afterhours"})
    result = await g.run(ctx)
    assert result.status == "skip"
```

- [ ] **Step 8.2: Run, verify failure**

- [ ] **Step 8.3: Implement**

```python
# skills/execution/rth_entry_guard.py
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class RthEntryGuard(Skill):
    name = "RthEntryGuard"

    async def run(self, ctx: Context) -> SkillResult:
        session = ctx.get("execution_session")
        if session != "rth":
            return SkillResult(status="skip",
                               reason=f"entry_outside_rth:{session}")
        return SkillResult(status="success")
```

- [ ] **Step 8.4: Run, verify pass; commit**

```bash
git add skills/execution/rth_entry_guard.py tests/unit/test_rth_entry_guard.py
git commit -m "skill: RthEntryGuard drops non-RTH entries"
```

---

## Task 9: EquityContractBuilder

**Files:**
- Create: `skills/execution/equity_contract_builder.py`
- Test: `tests/unit/test_equity_contract_builder.py`

- [ ] **Step 9.1: Write failing tests**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.equity_contract_builder import EquityContractBuilder
from agent.context import Context


@pytest.mark.asyncio
async def test_builds_qualified_stk_contract():
    gw = MagicMock()
    qualified = MagicMock()
    qualified.symbol = "AAPL"
    qualified.conId = 12345
    gw.qualify_equity = AsyncMock(return_value=qualified)
    builder = EquityContractBuilder(gw)
    ctx = Context()
    ctx.update({"ticker": "AAPL"})
    result = await builder.run(ctx)
    assert result.status == "success"
    selected = ctx.get("selected_contract")
    assert selected.symbol == "AAPL"
    assert selected.sec_type == "STK"
    assert selected.qualified is True
    assert selected.con_id == 12345
    assert ctx.get("instrument_type") == "equity"


@pytest.mark.asyncio
async def test_qualify_failure_returns_fail():
    from infra.ib.gateway import IBGatewayUnavailable
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(side_effect=IBGatewayUnavailable("nope"))
    builder = EquityContractBuilder(gw)
    ctx = Context()
    ctx.update({"ticker": "BADTKR"})
    result = await builder.run(ctx)
    assert result.status == "fail"
```

- [ ] **Step 9.2: Run, verify failure**

- [ ] **Step 9.3: Add `qualify_equity` helper to gateway (if absent)**

In `infra/ib/gateway.py`, near `get_quote`, add:

```python
    async def qualify_equity(self, ticker: str) -> BrokerContractRef:
        self._read_breaker.check()
        try:
            from ib_insync import Stock
            stock = Stock(ticker, "SMART", "USD")
            qualified = await self._ib.qualifyContractsAsync(stock)
            if not qualified:
                raise IBGatewayUnavailable(f"could not qualify equity {ticker}")
            q = qualified[0]
            ref = BrokerContractRef(
                symbol=q.symbol, sec_type="STK",
                exchange=q.exchange or "SMART",
                currency=q.currency or "USD",
                con_id=q.conId, qualified=True,
            )
            self._read_breaker._record_success()
            return ref
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"qualify_equity failed: {exc}") from exc
```

- [ ] **Step 9.4: Implement skill**

```python
# skills/execution/equity_contract_builder.py
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable


class EquityContractBuilder(Skill):
    name = "EquityContractBuilder"

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        if not ticker:
            return SkillResult(status="fail", reason="equity_contract_builder: ticker missing")
        try:
            ref = await self._gateway.qualify_equity(ticker)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable:{exc}")
        return SkillResult(status="success", updates={
            "selected_contract": ref,
            "selected_expiry": None,
            "selected_strike": None,
            "instrument_type": "equity",
        })
```

- [ ] **Step 9.5: Run, verify pass; commit**

```bash
git add skills/execution/equity_contract_builder.py infra/ib/gateway.py \
    tests/unit/test_equity_contract_builder.py
git commit -m "skill: EquityContractBuilder + gateway.qualify_equity"
```

---

## Task 10: ReferencePriceCapture

**Files:**
- Create: `skills/execution/reference_price_capture.py`
- Test: `tests/unit/test_reference_price_capture.py`

- [ ] **Step 10.1: Write failing tests**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.reference_price_capture import ReferencePriceCapture
from agent.context import Context


@pytest.mark.asyncio
async def test_captures_quote_into_context():
    gw = MagicMock()
    gw.get_quote = AsyncMock(return_value=147.32)
    skill = ReferencePriceCapture(gw)
    ctx = Context()
    ctx.update({"ticker": "AAPL"})
    result = await skill.run(ctx)
    assert result.status == "success"
    assert ctx.get("reference_price") == 147.32


@pytest.mark.asyncio
async def test_quote_failure_aborts_chain():
    from infra.ib.gateway import IBGatewayUnavailable
    gw = MagicMock()
    gw.get_quote = AsyncMock(side_effect=IBGatewayUnavailable("no quote"))
    skill = ReferencePriceCapture(gw)
    ctx = Context()
    ctx.update({"ticker": "AAPL"})
    result = await skill.run(ctx)
    assert result.status == "fail"
    assert "reference_price_unavailable" in (result.reason or "")
```

- [ ] **Step 10.2: Run, verify failure**

- [ ] **Step 10.3: Implement**

```python
# skills/execution/reference_price_capture.py
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable


class ReferencePriceCapture(Skill):
    name = "ReferencePriceCapture"

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        try:
            price = await self._gateway.get_quote(ticker)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail",
                               reason=f"reference_price_unavailable:{exc}")
        return SkillResult(status="success", updates={"reference_price": price})
```

- [ ] **Step 10.4: Run, verify pass; commit**

```bash
git add skills/execution/reference_price_capture.py \
    tests/unit/test_reference_price_capture.py
git commit -m "skill: ReferencePriceCapture snapshots quote at signal time"
```

---

## Task 11: SizingResolver

**Files:**
- Create: `skills/execution/sizing_resolver.py`
- Test: `tests/unit/test_sizing_resolver.py`

- [ ] **Step 11.1: Write failing tests**

```python
import pytest
from skills.execution.sizing_resolver import SizingResolver
from agent.policy import (PolicyModel, SizingPolicy, SizingBuckets, SizingTier,
                          ExecutionPolicy)
from agent.context import Context


def _policy_with_sizing() -> ExecutionPolicy:
    return ExecutionPolicy(
        sizing=SizingPolicy(
            default=SizingBuckets(
                high=SizingTier(shares=0.10, options=0.05),
                low=SizingTier(shares=0.05, options=0.05),
            ),
            per_channel={
                "stock-talk-portfolio": SizingBuckets(
                    high=SizingTier(shares=0.20, options=0.05),
                    low=SizingTier(shares=0.15, options=0.05),
                ),
                "mystic": SizingBuckets(
                    high=SizingTier(shares=0.15, options=0.05),
                    low=SizingTier(shares=0.10, options=0.05),
                ),
            },
        ),
    )


@pytest.mark.asyncio
async def test_per_channel_high():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context()
    ctx.update({"channel": "stock-talk-portfolio", "bucket": "HIGH"})
    await skill.run(ctx)
    assert ctx.get("shares_pct") == 0.20
    assert ctx.get("options_pct") == 0.05


@pytest.mark.asyncio
async def test_per_channel_low():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context()
    ctx.update({"channel": "mystic", "bucket": "LOW"})
    await skill.run(ctx)
    assert ctx.get("shares_pct") == 0.10
    assert ctx.get("options_pct") == 0.05


@pytest.mark.asyncio
async def test_default_for_unknown_channel():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context()
    ctx.update({"channel": "urkel", "bucket": "HIGH"})
    await skill.run(ctx)
    assert ctx.get("shares_pct") == 0.10
    assert ctx.get("options_pct") == 0.05


@pytest.mark.asyncio
async def test_skip_bucket_terminates():
    skill = SizingResolver(_policy_with_sizing())
    ctx = Context()
    ctx.update({"channel": "mystic", "bucket": "SKIP"})
    result = await skill.run(ctx)
    assert result.status == "skip"
```

- [ ] **Step 11.2: Run, verify failure**

- [ ] **Step 11.3: Implement**

```python
# skills/execution/sizing_resolver.py
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import ExecutionPolicy


class SizingResolver(Skill):
    name = "SizingResolver"

    def __init__(self, execution_policy: ExecutionPolicy) -> None:
        self._policy = execution_policy

    async def run(self, ctx: Context) -> SkillResult:
        bucket = ctx.get("bucket")
        if bucket == "SKIP" or bucket is None:
            return SkillResult(status="skip", reason=f"sizing_resolver: bucket={bucket}")
        channel = ctx.get("channel")
        sz = self._policy.sizing
        buckets = sz.per_channel.get(channel, sz.default)
        tier = buckets.high if bucket == "HIGH" else buckets.low
        return SkillResult(status="success", updates={
            "shares_pct": tier.shares,
            "options_pct": tier.options,
        })
```

- [ ] **Step 11.4: Run, verify pass; commit**

```bash
git add skills/execution/sizing_resolver.py tests/unit/test_sizing_resolver.py
git commit -m "skill: SizingResolver looks up per-channel sizing from policy"
```

---

## Task 12: OrderSizer rework — NetLiq × multiplier, dual-key reading

**Files:**
- Modify: `skills/execution/order_sizer.py`
- Modify: `tests/unit/test_order_sizer.py`
- Test: `tests/unit/test_order_sizer_netliq.py` (new)

- [ ] **Step 12.1: Write failing tests**

```python
# tests/unit/test_order_sizer_netliq.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from infra.ib.models import AccountSummary
from skills.execution.order_sizer import OrderSizer
from agent.context import Context


@pytest.fixture
def gateway():
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=99999, net_liquidation=100_000.0, currency="USD",
    ))
    gw.get_quote = AsyncMock(return_value=200.0)
    return gw


@pytest.mark.asyncio
async def test_equity_uses_shares_pct_and_netliq_x_multiplier(gateway):
    sizer = OrderSizer(gateway, margin_multiplier=2.0)
    ctx = Context()
    ctx.update({
        "ticker": "AAPL", "instrument_type": "equity",
        "shares_pct": 0.10, "options_pct": 0.05,
    })
    await sizer.run(ctx)
    # base = 100000 * 2.0 = 200000; alloc = 200000 * 0.10 = 20000; px=200 → qty=100
    assert ctx.get("quantity") == 100
    assert ctx.get("notional_estimate") == pytest.approx(20000.0)


@pytest.mark.asyncio
async def test_option_uses_options_pct(gateway):
    sizer = OrderSizer(gateway, margin_multiplier=2.0)
    candidate = MagicMock(strike=180.0, ask=5.0, multiplier=100)
    ctx = Context()
    ctx.update({
        "ticker": "AAPL", "instrument_type": "option",
        "shares_pct": 0.10, "options_pct": 0.05,
        "option_candidates": [candidate], "selected_strike": 180.0,
    })
    await sizer.run(ctx)
    # base = 200000; alloc = 200000 * 0.05 = 10000; cost = 5*100 = 500; qty = 20
    assert ctx.get("quantity") == 20
```

- [ ] **Step 12.2: Run, verify failure**

- [ ] **Step 12.3: Rewrite `skills/execution/order_sizer.py`**

```python
from __future__ import annotations
import math
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class OrderSizer(Skill):
    name = "OrderSizer"

    def __init__(self, gateway, *, margin_multiplier: float = 2.0) -> None:
        self._gateway = gateway
        self._margin_multiplier = margin_multiplier

    async def run(self, ctx: Context) -> SkillResult:
        instrument_type = ctx.get("instrument_type", "option")
        size_pct = (ctx.get("shares_pct") if instrument_type == "equity"
                    else ctx.get("options_pct"))
        if size_pct is None or size_pct <= 0:
            return SkillResult(status="fail",
                               reason=f"order_sizer: pct missing for {instrument_type}")

        try:
            account = await self._gateway.get_account_summary()
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        sizing_base = account.net_liquidation * self._margin_multiplier
        allocation = sizing_base * size_pct

        if instrument_type == "option":
            candidates = ctx.get("option_candidates", [])
            selected_strike = ctx.get("selected_strike")
            matching = [c for c in candidates if c.strike == selected_strike]
            if not matching:
                return SkillResult(status="fail",
                                   reason="order_sizer: no matching option candidate")
            cand = matching[0]
            cost_per_contract = cand.ask * cand.multiplier
            quantity = math.floor(allocation / cost_per_contract)
            notional = quantity * cost_per_contract
            ask = cand.ask
        else:
            ticker = ctx.get("ticker")
            try:
                ask = await self._gateway.get_quote(ticker)
            except IBGatewayUnavailable as exc:
                return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")
            quantity = math.floor(allocation / ask)
            notional = quantity * ask

        if quantity < 1:
            return SkillResult(status="fail",
                               reason=f"insufficient_buying_power: alloc={allocation:.2f} < 1 unit at {ask}")

        reason = (f"{instrument_type} pct={size_pct:.4f} of "
                  f"NetLiq=${account.net_liquidation:,.0f} × {self._margin_multiplier}")
        logger.info("OrderSizer: qty=%d notional=%.2f (%s)", quantity, notional, reason)
        return SkillResult(status="success", updates={
            "quantity": quantity,
            "notional_estimate": notional,
            "sizing_reason": reason,
            "capped_by": None,
        })
```

- [ ] **Step 12.4: Update existing `tests/unit/test_order_sizer.py`**

Where existing tests pass `size_pct` directly into ctx, replace with `shares_pct` (for equity) or `options_pct` (for option). Where tests assert sizing off `buying_power`, change the mock's `net_liquidation` and assert against `net_liq × 2.0`.

- [ ] **Step 12.5: Run all order-sizer tests, verify pass**

- [ ] **Step 12.6: Commit**

```bash
git add skills/execution/order_sizer.py tests/unit/test_order_sizer.py \
    tests/unit/test_order_sizer_netliq.py
git commit -m "order_sizer: NetLiq×multiplier base + shares_pct/options_pct dual-read"
```

---

## Task 13: TradeIntentWriter — dynamic instrument_type + parent_intent_id

**Files:**
- Modify: `skills/execution/trade_intent_writer.py`
- Modify: `tests/unit/test_trade_intent_writer.py`
- Modify: `infra/storage/trade_intent_store.py` (if needed for new column)

- [ ] **Step 13.1: Inspect current writer to understand the diff**

Read `skills/execution/trade_intent_writer.py` first; the existing line ~45 hardcodes `"instrument_type": "option"`. Also inspect `trade_intent_store.py` to find where the row is INSERTed and add `fill_qty` and `parent_intent_id` columns to the INSERT statement.

- [ ] **Step 13.2: Write failing tests**

```python
import pytest
from agent.context import Context
from skills.execution.trade_intent_writer import TradeIntentWriter


@pytest.mark.asyncio
async def test_writes_equity_when_ctx_says_equity(fake_intent_store):
    writer = TradeIntentWriter(fake_intent_store)
    ctx = Context()
    ctx.update({
        "trace_id": "t1", "event_id": "e1",
        "channel": "mystic", "ticker": "AAPL",
        "side": "long", "bucket": "HIGH",
        "instrument_type": "equity",
    })
    await writer.run(ctx)
    row = fake_intent_store.last_written
    assert row["instrument_type"] == "equity"
    assert row["parent_intent_id"] is None


@pytest.mark.asyncio
async def test_writes_option_with_parent_intent_id(fake_intent_store):
    writer = TradeIntentWriter(fake_intent_store)
    ctx = Context()
    ctx.update({
        "trace_id": "t1", "event_id": "e1",
        "channel": "mystic", "ticker": "AAPL",
        "side": "long", "bucket": "HIGH",
        "instrument_type": "option",
        "parent_intent_id": "shares-intent-123",
    })
    await writer.run(ctx)
    row = fake_intent_store.last_written
    assert row["instrument_type"] == "option"
    assert row["parent_intent_id"] == "shares-intent-123"
```

(Use the project's existing `fake_intent_store` fixture if present, or create one in conftest mirroring how prior tests stub the store.)

- [ ] **Step 13.3: Run, verify failure**

- [ ] **Step 13.4: Modify writer**

In `skills/execution/trade_intent_writer.py`, find the line that hardcodes `"instrument_type": "option"` and replace with `ctx.get("instrument_type", "equity")`. Add `parent_intent_id` to the dict written to the store, sourcing from `ctx.get("parent_intent_id")` (defaults to None).

- [ ] **Step 13.5: Modify `infra/storage/trade_intent_store.py` INSERT**

Add `parent_intent_id` and `fill_qty` columns to the INSERT statement and to any UPDATE statements used downstream (notably whatever path the SharesMarketSubmitter will use to record fills in Task 14). If there's no `update_fill` method that takes `fill_qty`, add one:

```python
    async def update_fill(self, intent_id: str, *, fill_price: float, fill_qty: int,
                          execution_state: str = "filled") -> None:
        await self._conn.execute(
            "UPDATE trade_intents SET fill_price=?, fill_qty=?, execution_state=?, "
            "updated_at=? WHERE intent_id=?",
            (fill_price, fill_qty, execution_state, _now(), intent_id),
        )
        await self._conn.commit()
```

- [ ] **Step 13.6: Run, verify pass; commit**

```bash
git add skills/execution/trade_intent_writer.py infra/storage/trade_intent_store.py \
    tests/unit/test_trade_intent_writer.py
git commit -m "intent: dynamic instrument_type + parent_intent_id + update_fill(fill_qty)"
```

---

## Task 14: SharesMarketSubmitter

**Files:**
- Create: `skills/execution/shares_market_submitter.py`
- Test: `tests/unit/test_shares_market_submitter.py`

- [ ] **Step 14.1: Write failing tests**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.shares_market_submitter import SharesMarketSubmitter
from agent.context import Context
from infra.ib.models import (PreparedOrder, FillResult, FillStatus)


@pytest.fixture
def submitter_deps():
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=100, remaining_qty=0,
        avg_fill_price=147.50, last_status="Filled",
        status_timestamp="2026-05-05T13:30:00Z",
    ))
    intent_store = MagicMock()
    intent_store.update_fill = AsyncMock()
    trim_store = MagicMock()
    trim_store.arm = AsyncMock()
    return gw, intent_store, trim_store


@pytest.mark.asyncio
async def test_long_signal_places_mkt_and_arms_trims(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    rungs = [(1, 0.05, 0.40), (2, 0.10, 0.40)]
    sub = SharesMarketSubmitter(gw, intent_store, trim_store,
                                  fill_timeout=5.0, trim_rungs=rungs)
    ctx = Context()
    ctx.update({
        "trace_id": "t1", "event_id": "e1",
        "intent_id": "intent-1", "ticker": "AAPL",
        "side": "long", "quantity": 100,
        "selected_contract": MagicMock(qualified=True, symbol="AAPL"),
    })
    result = await sub.run(ctx)
    assert result.status == "success"
    placed_order = gw.place_order.call_args[0][1]
    assert placed_order.order_type == "MKT"
    assert placed_order.action == "BUY"
    intent_store.update_fill.assert_awaited_once()
    args = intent_store.update_fill.call_args.kwargs
    assert args["fill_qty"] == 100
    assert args["fill_price"] == 147.50
    trim_store.arm.assert_awaited_once()
    arm_args = trim_store.arm.call_args
    assert arm_args.kwargs["rungs"] == rungs


@pytest.mark.asyncio
async def test_short_signal_skipped_no_orders(submitter_deps):
    gw, intent_store, trim_store = submitter_deps
    sub = SharesMarketSubmitter(gw, intent_store, trim_store,
                                  fill_timeout=5.0, trim_rungs=[])
    ctx = Context()
    ctx.update({
        "trace_id": "t1", "event_id": "e1",
        "intent_id": "intent-1", "ticker": "AAPL",
        "side": "short", "quantity": 100,
        "selected_contract": MagicMock(qualified=True),
    })
    result = await sub.run(ctx)
    assert result.status == "skip"
    assert "unsupported_short_signal" in (result.reason or "")
    gw.place_order.assert_not_awaited()
    trim_store.arm.assert_not_awaited()
```

- [ ] **Step 14.2: Run, verify failure**

- [ ] **Step 14.3: Implement**

```python
# skills/execution/shares_market_submitter.py
from __future__ import annotations
from datetime import datetime, timezone
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class SharesMarketSubmitter(Skill):
    name = "SharesMarketSubmitter"

    def __init__(self, gateway, intent_store, trim_store,
                 *, fill_timeout: float, trim_rungs: list[tuple[int, float, float]]):
        self._gateway = gateway
        self._intents = intent_store
        self._trims = trim_store
        self._timeout = fill_timeout
        self._rungs = trim_rungs

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("side") == "short":
            return SkillResult(status="skip", reason="unsupported_short_signal")

        contract = ctx.get("selected_contract")
        qty = ctx.get("quantity")
        if not contract or not contract.qualified or not qty or qty < 1:
            return SkillResult(status="fail", reason="shares_submit: missing contract/qty")

        order = PreparedOrder(action="BUY", quantity=qty, order_type="MKT",
                              limit_price=None, tif="DAY")
        client_order_id = f"{ctx.get('trace_id')}:shares:{ctx.get('event_id')}"
        try:
            trade = await self._gateway.place_order(contract, order, client_order_id)
            fill = await self._gateway.wait_fill(trade, timeout=self._timeout)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable:{exc}")

        if fill.status != FillStatus.FILLED:
            return SkillResult(status="fail",
                               reason=f"shares_not_filled:{fill.last_status}")

        intent_id = ctx.get("intent_id")
        await self._intents.update_fill(
            intent_id, fill_price=fill.avg_fill_price or 0.0,
            fill_qty=fill.filled_qty,
        )
        if self._rungs:
            await self._trims.arm(
                intent_id, rungs=self._rungs,
                armed_at=datetime.now(timezone.utc).isoformat(),
            )
        return SkillResult(status="success", updates={
            "shares_intent_id": intent_id,
            "shares_fill_price": fill.avg_fill_price,
            "shares_fill_qty": fill.filled_qty,
        })
```

- [ ] **Step 14.4: Run, verify pass; commit**

```bash
git add skills/execution/shares_market_submitter.py \
    tests/unit/test_shares_market_submitter.py
git commit -m "skill: SharesMarketSubmitter MKT BUY + arm trim ladder"
```

---

## Task 15: OptionsChaseGuard

**Files:**
- Create: `skills/execution/options_chase_guard.py`
- Test: `tests/unit/test_options_chase_guard.py`

- [ ] **Step 15.1: Write failing tests**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from skills.execution.options_chase_guard import OptionsChaseGuard
from agent.context import Context


def _gw(price):
    g = MagicMock()
    g.get_quote = AsyncMock(return_value=price)
    return g


@pytest.mark.asyncio
async def test_passes_when_within_threshold():
    g = OptionsChaseGuard(_gw(109.0), threshold_pct=0.10)
    ctx = Context()
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_passes_at_boundary():
    g = OptionsChaseGuard(_gw(110.0), threshold_pct=0.10)
    ctx = Context()
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_skips_above_threshold():
    g = OptionsChaseGuard(_gw(111.0), threshold_pct=0.10)
    ctx = Context()
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "skip"
    assert "options_chase_skip" in (result.reason or "")


@pytest.mark.asyncio
async def test_quote_failure_skips_options():
    """If we can't quote, skip the options leg rather than fail the whole chain."""
    from infra.ib.gateway import IBGatewayUnavailable
    gw = MagicMock()
    gw.get_quote = AsyncMock(side_effect=IBGatewayUnavailable("nope"))
    g = OptionsChaseGuard(gw, threshold_pct=0.10)
    ctx = Context()
    ctx.update({"ticker": "AAPL", "reference_price": 100.0})
    result = await g.run(ctx)
    assert result.status == "skip"
```

- [ ] **Step 15.2: Run, verify failure**

- [ ] **Step 15.3: Implement**

```python
# skills/execution/options_chase_guard.py
from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class OptionsChaseGuard(Skill):
    name = "OptionsChaseGuard"

    def __init__(self, gateway, *, threshold_pct: float) -> None:
        self._gateway = gateway
        self._threshold = threshold_pct

    async def run(self, ctx: Context) -> SkillResult:
        ref = ctx.get("reference_price")
        if ref is None or ref <= 0:
            return SkillResult(status="skip", reason="options_chase_skip:no_reference")
        ticker = ctx.get("ticker")
        try:
            current = await self._gateway.get_quote(ticker)
        except IBGatewayUnavailable as exc:
            logger.warning("OptionsChaseGuard: quote failed (%s); skipping options", exc)
            return SkillResult(status="skip",
                               reason=f"options_chase_skip:quote_unavailable:{exc}")
        ratio = current / ref
        if ratio > 1.0 + self._threshold:
            return SkillResult(status="skip",
                               reason=f"options_chase_skip: current={current} > ref={ref}×{1+self._threshold}")
        return SkillResult(status="success", updates={"options_current_price": current})
```

- [ ] **Step 15.4: Run, verify pass; commit**

```bash
git add skills/execution/options_chase_guard.py tests/unit/test_options_chase_guard.py
git commit -m "skill: OptionsChaseGuard skips options leg on >10% chase"
```

---

## Task 16: OptionsMarketSubmitter

**Files:**
- Create: `skills/execution/options_market_submitter.py`
- Test: `tests/unit/test_options_market_submitter.py`

- [ ] **Step 16.1: Write failing tests**

```python
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock
from skills.execution.options_market_submitter import OptionsMarketSubmitter
from agent.context import Context
from infra.ib.models import FillResult, FillStatus


@pytest.fixture
def deps():
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="opt-1", perm_id=2,
        submitted_qty=20, filled_qty=20, remaining_qty=0, avg_fill_price=5.10,
        last_status="Filled", status_timestamp="2026-05-05T13:31:00Z",
    ))
    intent_store = MagicMock()
    intent_store.write = AsyncMock()
    return gw, intent_store


@pytest.mark.asyncio
async def test_writes_options_intent_with_parent_link(deps):
    gw, intent_store = deps
    sub = OptionsMarketSubmitter(gw, intent_store, fill_timeout=5.0)
    ctx = Context()
    ctx.update({
        "trace_id": "t1", "event_id": "e1",
        "shares_intent_id": "shares-intent-1",
        "ticker": "AAPL", "side": "long", "quantity": 20,
        "selected_contract": MagicMock(qualified=True, sec_type="OPT", symbol="AAPL"),
        "selected_strike": 180.0, "selected_expiry": "2026-12-18",
    })
    result = await sub.run(ctx)
    assert result.status == "success"
    write_kwargs = intent_store.write.call_args.kwargs
    assert write_kwargs["instrument_type"] == "option"
    assert write_kwargs["parent_intent_id"] == "shares-intent-1"
    placed = gw.place_order.call_args[0][1]
    assert placed.order_type == "MKT"


@pytest.mark.asyncio
async def test_short_signal_skipped(deps):
    gw, intent_store = deps
    sub = OptionsMarketSubmitter(gw, intent_store, fill_timeout=5.0)
    ctx = Context()
    ctx.update({
        "shares_intent_id": "x", "ticker": "AAPL", "side": "short",
        "quantity": 10, "selected_contract": MagicMock(qualified=True),
    })
    result = await sub.run(ctx)
    assert result.status == "skip"
    gw.place_order.assert_not_awaited()
```

- [ ] **Step 16.2: Run, verify failure**

- [ ] **Step 16.3: Implement**

```python
# skills/execution/options_market_submitter.py
from __future__ import annotations
import uuid
from datetime import datetime, timezone
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class OptionsMarketSubmitter(Skill):
    name = "OptionsMarketSubmitter"

    def __init__(self, gateway, intent_store, *, fill_timeout: float):
        self._gateway = gateway
        self._intents = intent_store
        self._timeout = fill_timeout

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("side") == "short":
            return SkillResult(status="skip", reason="unsupported_short_signal")

        contract = ctx.get("selected_contract")
        qty = ctx.get("quantity")
        if not contract or not contract.qualified or not qty or qty < 1:
            return SkillResult(status="fail", reason="options_submit: missing contract/qty")

        order = PreparedOrder(action="BUY", quantity=qty, order_type="MKT",
                              limit_price=None, tif="DAY")
        client_order_id = f"{ctx.get('trace_id')}:options:{ctx.get('event_id')}"
        try:
            trade = await self._gateway.place_order(contract, order, client_order_id)
            fill = await self._gateway.wait_fill(trade, timeout=self._timeout)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable:{exc}")

        if fill.status != FillStatus.FILLED:
            return SkillResult(status="fail",
                               reason=f"options_not_filled:{fill.last_status}")

        options_intent_id = str(uuid.uuid4())
        await self._intents.write(
            intent_id=options_intent_id,
            event_id=ctx.get("event_id"),
            channel=ctx.get("channel"),
            ticker=ctx.get("ticker"),
            side=ctx.get("side"),
            instrument_type="option",
            parent_intent_id=ctx.get("shares_intent_id"),
            expiry=ctx.get("selected_expiry"),
            strike=ctx.get("selected_strike"),
            right="C",
            conviction=ctx.get("bucket"),
            fill_price=fill.avg_fill_price,
            fill_qty=fill.filled_qty,
            execution_state="filled",
            signal_received_at=ctx.get("signal_received_at"),
        )
        return SkillResult(status="success", updates={
            "options_intent_id": options_intent_id,
            "options_fill_price": fill.avg_fill_price,
            "options_fill_qty": fill.filled_qty,
        })
```

(If `intent_store.write` does not exist with this signature, add it to `infra/storage/trade_intent_store.py` — a thin INSERT for an already-filled options row.)

- [ ] **Step 16.4: Run, verify pass; commit**

```bash
git add skills/execution/options_market_submitter.py \
    infra/storage/trade_intent_store.py \
    tests/unit/test_options_market_submitter.py
git commit -m "skill: OptionsMarketSubmitter writes child intent with parent_intent_id"
```

---

## Task 17: ExitLadder background task

**Files:**
- Create: `agent/exit_ladder.py`
- Test: `tests/unit/test_exit_ladder.py`

- [ ] **Step 17.1: Write failing tests for the core polling/firing logic**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.exit_ladder import fire_rung_if_crossed
from infra.ib.models import FillResult, FillStatus


@pytest.mark.asyncio
async def test_does_not_fire_below_threshold():
    gw = MagicMock(); intents = MagicMock(); trims = MagicMock()
    trims.record_fire = AsyncMock()
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=104.0,
    )
    assert fired is False
    trims.record_fire.assert_not_awaited()


@pytest.mark.asyncio
async def test_fires_at_threshold_and_records():
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=MagicMock(qualified=True))
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="sell-1", perm_id=9,
        submitted_qty=40, filled_qty=40, remaining_qty=0, avg_fill_price=105.10,
        last_status="Filled", status_timestamp="2026-05-05T14:00:00Z",
    ))
    trims = MagicMock(); trims.record_fire = AsyncMock()
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=100,
        rung=1, threshold_pct=0.05, trim_pct=0.40,
        current_price=105.0,
    )
    assert fired is True
    placed = gw.place_order.call_args[0][1]
    assert placed.order_type == "MKT"
    assert placed.action == "SELL"
    assert placed.quantity == 40
    trims.record_fire.assert_awaited_once()


@pytest.mark.asyncio
async def test_rounds_trim_qty_minimum_one():
    """A 4% trim on 11 shares = round_half_up(0.44) = 4 shares (existing math),
    but if trim_pct=0.05 and original_qty=10 → round(0.5) = 0 — bump to 1."""
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=MagicMock(qualified=True))
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="sell-2", perm_id=10,
        submitted_qty=1, filled_qty=1, remaining_qty=0, avg_fill_price=105.0,
        last_status="Filled", status_timestamp="2026-05-05T14:00:00Z",
    ))
    trims = MagicMock(); trims.record_fire = AsyncMock()
    fired = await fire_rung_if_crossed(
        gw=gw, trim_store=trims,
        intent_id="i1", ticker="AAPL",
        avg_fill_price=100.0, original_qty=2, rung=1,
        threshold_pct=0.05, trim_pct=0.40, current_price=105.0,
    )
    assert fired is True
    placed = gw.place_order.call_args[0][1]
    assert placed.quantity >= 1
```

- [ ] **Step 17.2: Run, verify failure**

- [ ] **Step 17.3: Implement**

```python
# agent/exit_ladder.py
from __future__ import annotations
import asyncio
import logging
import math
from datetime import datetime, timezone
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


def _round_half_up_min1(n: float) -> int:
    rounded = int(math.floor(n + 0.5))
    return max(1, rounded)


async def fire_rung_if_crossed(
    *, gw, trim_store, intent_id: str, ticker: str,
    avg_fill_price: float, original_qty: int,
    rung: int, threshold_pct: float, trim_pct: float,
    current_price: float,
) -> bool:
    threshold_price = avg_fill_price * (1.0 + threshold_pct)
    if current_price < threshold_price:
        return False

    trim_qty = _round_half_up_min1(original_qty * trim_pct)
    contract = await gw.qualify_equity(ticker)
    order = PreparedOrder(action="SELL", quantity=trim_qty, order_type="MKT",
                          limit_price=None, tif="DAY")
    client_order_id = f"{intent_id}:trim:R{rung}"
    try:
        trade = await gw.place_order(contract, order, client_order_id)
        fill = await gw.wait_fill(trade, timeout=30.0)
    except IBGatewayUnavailable as exc:
        logger.error("trim sell broker unavailable: %s", exc)
        return False

    await trim_store.record_fire(
        intent_id=intent_id, rung=rung,
        fired_at=datetime.now(timezone.utc).isoformat(),
        fire_price=current_price,
        sold_qty=fill.filled_qty if fill.status == FillStatus.FILLED else 0,
        sold_avg_price=fill.avg_fill_price,
        broker_order_ref=fill.broker_order_id,
    )
    return True


class ExitLadder:
    def __init__(self, gateway, intent_store, trim_store, *,
                 poll_interval_seconds: int):
        self._gw = gateway
        self._intents = intent_store
        self._trims = trim_store
        self._interval = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception:
                logger.exception("exit ladder tick failed")
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        # Only fire during RTH; cheap clock check
        from datetime import datetime as dt
        now = dt.now()
        h, m = now.hour, now.minute
        in_rth = (h == 9 and m >= 30) or (10 <= h < 16)
        if not in_rth:
            return

        unfired = await self._trims.all_unfired()
        # Group by intent_id; for each intent, fetch position + quote, walk rungs in order
        from collections import defaultdict
        by_intent = defaultdict(list)
        for row in unfired:
            by_intent[row["intent_id"]].append(row)

        for intent_id, rungs in by_intent.items():
            intent = await self._intents.get(intent_id)
            if not intent or intent["execution_state"] != "filled":
                continue
            try:
                current_price = await self._gw.get_quote(intent["ticker"])
            except IBGatewayUnavailable:
                continue
            for r in sorted(rungs, key=lambda x: x["rung"]):
                fired = await fire_rung_if_crossed(
                    gw=self._gw, trim_store=self._trims,
                    intent_id=intent_id, ticker=intent["ticker"],
                    avg_fill_price=intent["fill_price"],
                    original_qty=intent["fill_qty"],
                    rung=r["rung"], threshold_pct=r["threshold_pct"],
                    trim_pct=r["trim_pct"],
                    current_price=current_price,
                )
                if not fired:
                    break  # rungs are ordered; if R1 didn't fire, R2 won't either
```

- [ ] **Step 17.4: Run, verify pass; commit**

```bash
git add agent/exit_ladder.py tests/unit/test_exit_ladder.py
git commit -m "agent: ExitLadder background poll/fire with rung crossing logic"
```

---

## Task 18: Wire ExitLadder into `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 18.1: Inspect `main.py`** to find where the existing reconciler is started; add the ExitLadder alongside.

- [ ] **Step 18.2: Add ExitLadder bootstrap**

After the reconciler bootstrap (search for `Reconciler` or `reconciler` in `main.py`):

```python
from agent.exit_ladder import ExitLadder
from infra.storage.trim_ladder_store import TrimLadderStore

trim_store = TrimLadderStore(intent_store_conn)  # reuse the trade-intents conn
exit_ladder = ExitLadder(
    gateway=gateway, intent_store=intent_store, trim_store=trim_store,
    poll_interval_seconds=policy.execution.exit_poll_interval_seconds,
)
exit_ladder.start()
# … on shutdown:
# await exit_ladder.stop()
```

Hook `await exit_ladder.stop()` into whatever shutdown path exists (KeyboardInterrupt, signal handler).

- [ ] **Step 18.3: Smoke-test by running `main.py` and confirming no import or boot errors**

`python -m main --check` (or whatever the project's dry-run flag is; if absent, briefly run and Ctrl-C). Logs should show `ExitLadder` starting.

- [ ] **Step 18.4: Commit**

```bash
git add main.py
git commit -m "main: start ExitLadder background task"
```

---

## Task 19: Rebuild `build_phase2b_execution_chain` in `agent/registry.py`

**Files:**
- Modify: `agent/registry.py`
- Test: `tests/unit/test_phase2b_chain.py` (new)

- [ ] **Step 19.1: Write failing test**

```python
def test_chain_has_expected_skill_order(policy_with_sizing, gateway, stores):
    from agent.registry import build_phase2b_execution_chain
    chain = build_phase2b_execution_chain(
        policy=policy_with_sizing,
        execution_store=stores["execution"],
        gateway=gateway,
        trade_intent_store=stores["intents"],
        trim_store=stores["trims"],
    )
    names = [s.name for s in chain]
    assert names == [
        "TradeIntentWriter",          # Phase 1: intent persistence + guards
        "ChannelPolicyGuard",
        "CooldownGuard",
        "ExecutionEligibilityGuard",
        "RthEntryGuard",
        "ReferencePriceCapture",
        "SizingResolver",
        "EquityContractBuilder",      # shares sub-chain begins
        "OrderSizer",
        "SharesMarketSubmitter",
        "OptionsChaseGuard",          # gate to options sub-chain
        "ChainLookup",                # options sub-chain begins
        "InstrumentMarketabilityGuard",
        "ContractSelector",
        "OrderSizer",                 # second invocation, options branch
        "OptionsMarketSubmitter",
    ]
```

(Provide a fixture that builds a `PolicyModel` matching `policy.yaml`, and trivial mocks for stores/gateway. The test is purely structural — it asserts the chain wiring.)

- [ ] **Step 19.2: Run, verify failure**

- [ ] **Step 19.3: Rewrite `build_phase2b_execution_chain`**

Replace the existing function with:

```python
def build_phase2b_execution_chain(policy, execution_store, gateway,
                                   trade_intent_store=None,
                                   trim_store=None) -> list:
    from skills.execution.trade_intent_writer import TradeIntentWriter
    from skills.execution.channel_policy_guard import ChannelPolicyGuard
    from skills.execution.cooldown_guard import CooldownGuard
    from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
    from skills.execution.rth_entry_guard import RthEntryGuard
    from skills.execution.reference_price_capture import ReferencePriceCapture
    from skills.execution.sizing_resolver import SizingResolver
    from skills.execution.equity_contract_builder import EquityContractBuilder
    from skills.execution.order_sizer import OrderSizer
    from skills.execution.shares_market_submitter import SharesMarketSubmitter
    from skills.execution.options_chase_guard import OptionsChaseGuard
    from skills.execution.chain_lookup import ChainLookup
    from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
    from skills.execution.contract_selector import ContractSelector
    from skills.execution.options_market_submitter import OptionsMarketSubmitter

    intent_guards = []
    if trade_intent_store is not None:
        intent_guards = [
            TradeIntentWriter(trade_intent_store),
            ChannelPolicyGuard(policy, trade_intent_store),
            CooldownGuard(policy, trade_intent_store),
        ]

    rungs = [(i + 1, r.threshold_pct, r.trim_pct)
             for i, r in enumerate(policy.execution.trim_ladder.rungs)]

    return intent_guards + [
        ExecutionEligibilityGuard(policy),
        RthEntryGuard(),
        ReferencePriceCapture(gateway),
        SizingResolver(policy.execution),

        # Shares sub-chain
        EquityContractBuilder(gateway),
        OrderSizer(gateway, margin_multiplier=policy.execution.margin_multiplier),
        SharesMarketSubmitter(
            gateway, trade_intent_store, trim_store,
            fill_timeout=policy.execution.fill_wait_timeout_seconds,
            trim_rungs=rungs,
        ),

        # Options sub-chain (gated)
        OptionsChaseGuard(gateway,
                          threshold_pct=policy.execution.options_chase_threshold_pct),
        ChainLookup(gateway, execution_store._conn),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(gateway, margin_multiplier=policy.execution.margin_multiplier),
        OptionsMarketSubmitter(
            gateway, trade_intent_store,
            fill_timeout=policy.execution.fill_wait_timeout_seconds,
        ),
    ]
```

Note: `OrderSizer` needs to read `instrument_type` correctly in each branch. After `EquityContractBuilder` runs, ctx has `instrument_type="equity"`. After `ContractSelector` runs, ctx has `instrument_type="option"` (verify in `contract_selector.py:14-15` which already supports both). The same OrderSizer class is reused; the dual-key read in Task 12 makes this safe.

- [ ] **Step 19.4: Run, verify pass**

- [ ] **Step 19.5: Run the full test suite, fix any callers of `build_phase2b_execution_chain` whose signature changed (added `trim_store`)**

`pytest tests/ -x -q`

- [ ] **Step 19.6: Commit**

```bash
git add agent/registry.py tests/unit/test_phase2b_chain.py
git commit -m "registry: rebuild phase2b chain — shares-first + options-with-chase-guard"
```

---

## Task 20: Integration smoke test (paper account)

**Files:**
- Create: `tests/integration/test_shares_plus_options_e2e.py`

- [ ] **Step 20.1: Write the smoke test**

This test wires the full chain with mock gateway responses and verifies end-to-end:

```python
import pytest
import aiosqlite
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from agent.policy import load_policy
from agent.registry import build_phase2b_execution_chain
from infra.storage.db import SCHEMA
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from infra.ib.models import FillResult, FillStatus, BrokerContractRef


@pytest.mark.asyncio
async def test_high_signal_fires_shares_then_options(tmp_path):
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    intents = TradeIntentStore(db)
    trims = TrimLadderStore(db)

    gw = MagicMock()
    gw.get_quote = AsyncMock(side_effect=[100.0, 101.0])  # ref then post-shares
    gw.qualify_equity = AsyncMock(return_value=BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        qualified=True))
    from infra.ib.models import AccountSummary
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=200_000, net_liquidation=100_000, currency="USD"))
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(side_effect=[
        FillResult(FillStatus.FILLED, "shares-1", 1, 200, 200, 0, 100.0, "Filled", "t"),
        FillResult(FillStatus.FILLED, "options-1", 2, 50, 50, 0, 5.0, "Filled", "t"),
    ])
    # Stub out chain_lookup + contract_selector to inject a pre-built option candidate
    gw.fetch_option_chain = AsyncMock(return_value=[
        MagicMock(strike=95.0, ask=5.0, bid=4.95, mid=4.975, multiplier=100,
                  spread_pct=0.01, expiry="2026-12-18", right="C",
                  contract_ref=BrokerContractRef(
                      symbol="AAPL", sec_type="OPT", exchange="SMART",
                      currency="USD", strike=95.0, expiry="20261218",
                      right="C", qualified=True)),
    ])

    policy = load_policy("config/policy.yaml")
    execution_store = MagicMock()
    execution_store._conn = db
    chain = build_phase2b_execution_chain(
        policy, execution_store, gw,
        trade_intent_store=intents, trim_store=trims,
    )

    ctx = Context()
    ctx.update({
        "trace_id": "t-test", "event_id": "e-test", "intent_id": "intent-test",
        "channel": "stock-talk-portfolio", "ticker": "AAPL",
        "side": "long", "bucket": "HIGH",
        "execution_session": "rth", "spot_price": 100.0,
    })

    for skill in chain:
        result = await skill.run(ctx)
        if result.status in ("fail", "skip"):
            pytest.fail(f"chain stopped at {skill.name}: {result.reason}")
        if result.updates:
            ctx.update(result.updates)

    # Assertions
    assert ctx.get("shares_fill_qty") == 200
    assert ctx.get("options_fill_qty") == 50
    # Trim ladder armed
    rows = await trims.unfired_for_intent("intent-test")
    assert len(rows) == 2
```

- [ ] **Step 20.2: Run, verify pass**

- [ ] **Step 20.3: Commit**

```bash
git add tests/integration/test_shares_plus_options_e2e.py
git commit -m "test: end-to-end shares+options chain smoke test"
```

---

## Task 21: Update memory and operational docs

**Files:**
- Modify: `~/.claude/projects/-Users-jasonli-dev-trading-agent/memory/project_discord_capture_locked_in.md` (already updated when spec was written — verify it reflects the implementation having landed)

- [ ] **Step 21.1: Update the memory entry to remove "spec to land soon" hedging once all tests pass**

The memory was pre-updated with the corrected channel mapping. After landing, ensure the memory phrasing reads as state-of-the-world, not state-of-the-spec.

- [ ] **Step 21.2: Final agent restart smoke**

```bash
bin/agent-stop && bin/agent-start && sleep 5 && bin/agent-status
```

Verify all 4 services come up, no errors in agent logs about missing config keys or table columns.

- [ ] **Step 21.3: Commit any memory or doc tweaks**

```bash
git add docs/  # if any docs changed
git commit -m "docs: post-implementation cleanup"
```

---

## Spec Coverage Self-Check

Going section by section through the spec:

| Spec section | Tasks |
|---|---|
| Sizing matrix | 3 (Pydantic), 4 (yaml), 11 (resolver) |
| NetLiq × margin_multiplier base | 3 (Pydantic), 12 (OrderSizer) |
| Entry sequence (shares first → chase guard → options) | 19 (registry) integrating 8–16 |
| Reference price snapshot | 10 |
| Trim ladder (carryover, shares-only) | 1 (schema), 2 (store), 17 (ladder), 18 (main wiring), 14 (arming) |
| WSE small-size override | 6 |
| Channel ID swap + new channels | 4 (yaml) |
| New trader profile YAMLs | 5 |
| `parent_intent_id` column + linking | 1 (schema), 13 (writer), 16 (options submitter) |
| `instrument_policy.fallback_to_stock_if_no_options` removal | 3 (Pydantic), 4 (yaml) |
| `MAX_STATED_SIZE` removal | 6 |
| Dynamic `instrument_type` in trade_intent_writer | 13 |
| MKT support in gateway / PreparedOrder.limit_price optional | 7 |
| RthEntryGuard | 8 |
| EquityContractBuilder | 9 |
| OptionsChaseGuard | 15 |
| OptionsMarketSubmitter | 16 |
| `_assert_paper_guard` remains | unchanged — no task touches it |
| Idempotency keys (`:shares` vs `:options`) | 14, 16 (client_order_id format) |

No gaps. Plan is complete.
