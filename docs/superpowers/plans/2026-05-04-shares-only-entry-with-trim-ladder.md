# Shares-Only Entry with Trim Ladder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current option-or-LMT-walk entry path with a shares-only MKT entry sized at 80% (HIGH) / 40% (LOW) of buying power, plus a background trim ladder that sells 40% at +5% and another 40% at +10%, leaving 20% to hold forever.

**Architecture:** Modify the conviction classifier and trade-intent writer for the new sizing/instrument; rebuild the Phase 2b chain with three new skills (`RthEntryGuard`, `EquityContractBuilder`, `SharesMarketSubmitter`); add a background `ExitLadder` task that polls IB quotes and fires MKT trim sells when thresholds are crossed. State persisted in a new `trade_intent_trims` table so trim rungs survive agent restarts.

**Tech Stack:** Python 3 + asyncio, ib_insync (IB Gateway), aiosqlite (state), pydantic (config), pytest + pytest-asyncio (tests). Paper account only — `_assert_paper_guard` in `infra/ib/gateway.py:414` stays in force.

**Spec:** `docs/superpowers/specs/2026-05-04-shares-only-entry-with-trim-ladder-design.md`

---

## File Structure

**New files:**
- `skills/execution/rth_entry_guard.py` — drops non-RTH entries
- `skills/execution/equity_contract_builder.py` — qualifies STK contract
- `skills/execution/shares_market_submitter.py` — MKT BUY → arm trims
- `infra/storage/trim_ladder_store.py` — CRUD for `trade_intent_trims`
- `agent/exit_ladder.py` — background poll/fire loop
- `tests/unit/test_rth_entry_guard.py`
- `tests/unit/test_equity_contract_builder.py`
- `tests/unit/test_shares_market_submitter.py`
- `tests/unit/test_trim_ladder_store.py`
- `tests/unit/test_exit_ladder.py`
- `tests/unit/test_gateway_market_order.py`

**Modified files:**
- `infra/storage/db.py` — add `fill_qty` column, add `trade_intent_trims` table
- `infra/storage/trade_intent_store.py` — `update_execution_state` accepts `fill_qty`
- `agent/policy.py` — `ExecutionPolicy` adds `exit_poll_interval_seconds` and `trim_ladder`
- `config/policy.yaml` — add the new keys
- `infra/ib/models.py` — `PreparedOrder.limit_price: float | None`
- `infra/ib/gateway.py` — `place_order` branches on `order_type`
- `skills/signal/trader_classifier.py` — sizing constants + bucket-only shortcut
- `skills/execution/trade_intent_writer.py` — write `instrument_type="equity"`
- `agent/registry.py` — `build_phase2b_execution_chain` rewired to shares-only
- `main.py` — start the `ExitLadder` background task alongside the reconciler
- `tests/unit/test_trader_classifier.py` — update expected sizes
- `tests/unit/test_trade_intent_writer.py` — assert `instrument_type=="equity"`

**Bypassed (kept in repo, not in chain):** `chain_lookup.py`, `instrument_marketability_guard.py`, `contract_selector.py`, `order_pricer.py`, `price_walker.py`. Their tests stay green; we just don't wire them into the entry chain.

---

## Task 1: Schema additions

**Files:**
- Modify: `infra/storage/db.py`
- Test: `tests/unit/test_schema_trim_ladder.py` (new)

- [ ] **Step 1.1: Write the failing schema test**

Create `tests/unit/test_schema_trim_ladder.py`:

```python
import pytest
import aiosqlite
from infra.storage.db import SCHEMA


@pytest.mark.asyncio
async def test_trade_intents_has_fill_qty_column():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        async with conn.execute("PRAGMA table_info(trade_intents)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "fill_qty" in cols, "trade_intents must have fill_qty column"


@pytest.mark.asyncio
async def test_trade_intent_trims_table_exists_with_required_columns():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        async with conn.execute("PRAGMA table_info(trade_intent_trims)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        for required in [
            "intent_id", "rung", "threshold_pct", "trim_pct",
            "armed_at", "fired_at", "fire_price", "sold_qty",
            "sold_avg_price", "broker_order_ref",
        ]:
            assert required in cols, f"missing column {required}"


@pytest.mark.asyncio
async def test_trade_intent_trims_primary_key_is_intent_and_rung():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        await conn.execute(
            "INSERT INTO trade_intent_trims (intent_id, rung, threshold_pct, trim_pct, armed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e1:NVDA:long", 1, 0.05, 0.40, "2026-05-04T15:00:00+00:00"),
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO trade_intent_trims (intent_id, rung, threshold_pct, trim_pct, armed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("e1:NVDA:long", 1, 0.05, 0.40, "2026-05-04T15:00:00+00:00"),
            )
            await conn.commit()
```

- [ ] **Step 1.2: Run the test to verify it fails**

```bash
cd /Users/jasonli/dev/trading-agent
.venv/bin/pytest tests/unit/test_schema_trim_ladder.py -v
```

Expected: 3 failures — `fill_qty` missing, `trade_intent_trims` table doesn't exist.

- [ ] **Step 1.3: Modify the SCHEMA to add the column and table**

In `infra/storage/db.py`, locate the `trade_intents` CREATE TABLE block (lines 97–142). Add `fill_qty INTEGER,` immediately after the existing `fill_price REAL,` line.

Then immediately after the `dlq_intents` VIEW (around line 146), add:

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

- [ ] **Step 1.4: Run the schema tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_schema_trim_ladder.py -v
```

Expected: 3 passed.

- [ ] **Step 1.5: Run the full suite to confirm no regression**

```bash
.venv/bin/pytest -q
```

Expected: All previously-passing tests still pass.

- [ ] **Step 1.6: Commit**

```bash
git add infra/storage/db.py tests/unit/test_schema_trim_ladder.py
git commit -m "schema: add fill_qty + trade_intent_trims for trim ladder"
```

---

## Task 2: Policy additions

**Files:**
- Modify: `agent/policy.py:82-92` (`ExecutionPolicy`)
- Modify: `config/policy.yaml`
- Test: `tests/unit/test_policy_trim_ladder.py` (new)

- [ ] **Step 2.1: Write the failing policy test**

Create `tests/unit/test_policy_trim_ladder.py`:

```python
from agent.policy import load_policy


def test_policy_loads_exit_poll_interval_and_trim_ladder():
    policy = load_policy("config/policy.yaml")
    assert policy.execution.exit_poll_interval_seconds == 2
    assert len(policy.execution.trim_ladder.rungs) == 2

    r1, r2 = policy.execution.trim_ladder.rungs
    assert r1.threshold_pct == 0.05
    assert r1.trim_pct == 0.40
    assert r2.threshold_pct == 0.10
    assert r2.trim_pct == 0.40
```

- [ ] **Step 2.2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/unit/test_policy_trim_ladder.py -v
```

Expected: FAIL with `AttributeError` on `exit_poll_interval_seconds` / `trim_ladder`.

- [ ] **Step 2.3: Add the pydantic models**

In `agent/policy.py`, immediately above the `ExecutionPolicy` class (around line 82), add:

```python
class TrimRung(BaseModel):
    threshold_pct: float
    trim_pct: float


class TrimLadderConfig(BaseModel):
    rungs: list[TrimRung]
```

Then modify `ExecutionPolicy` (lines 82–92) to include the new fields. The full updated class:

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
    exit_poll_interval_seconds: int = 2
    trim_ladder: TrimLadderConfig = TrimLadderConfig(rungs=[
        TrimRung(threshold_pct=0.05, trim_pct=0.40),
        TrimRung(threshold_pct=0.10, trim_pct=0.40),
    ])
```

- [ ] **Step 2.4: Add the keys to `config/policy.yaml`**

In `config/policy.yaml`, find the `execution:` block (lines 81–90). Append to it (preserve indentation):

```yaml
  exit_poll_interval_seconds: 2
  trim_ladder:
    rungs:
      - threshold_pct: 0.05
        trim_pct: 0.40
      - threshold_pct: 0.10
        trim_pct: 0.40
```

- [ ] **Step 2.5: Run the test — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_policy_trim_ladder.py -v
```

Expected: PASS.

- [ ] **Step 2.6: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: all previously-passing tests still pass.

- [ ] **Step 2.7: Commit**

```bash
git add agent/policy.py config/policy.yaml tests/unit/test_policy_trim_ladder.py
git commit -m "policy: add exit_poll_interval and trim_ladder rungs"
```

---

## Task 3: PreparedOrder optional limit_price + Gateway MKT support

**Files:**
- Modify: `infra/ib/models.py:60-66`
- Modify: `infra/ib/gateway.py:319-348`
- Test: `tests/unit/test_gateway_market_order.py` (new)

- [ ] **Step 3.1: Write the failing gateway MKT test**

Create `tests/unit/test_gateway_market_order.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from infra.ib.gateway import IBGateway
from infra.ib.models import BrokerContractRef, PreparedOrder


def _stub_policy():
    p = MagicMock()
    p.ib_gateway.host = "127.0.0.1"
    p.ib_gateway.port = 4002
    p.ib_gateway.client_id = 1
    p.ib_gateway.mode = "paper"
    p.ib_gateway.paper_account_prefixes = ["DU"]
    return p


def _qualified_stk():
    return BrokerContractRef(
        symbol="NVDA", sec_type="STK", exchange="SMART", currency="USD",
        qualified=True,
    )


@pytest.mark.asyncio
async def test_place_order_mkt_builds_market_order():
    gateway = IBGateway(_stub_policy())
    fake_ib = MagicMock()
    fake_ib.placeOrder.return_value = MagicMock()
    gateway._ib = fake_ib
    gateway._account_id = "DU12345"  # bypass _assert_paper_guard

    order = PreparedOrder(action="BUY", quantity=10, order_type="MKT",
                          limit_price=None, tif="DAY")
    await gateway.place_order(_qualified_stk(), order, "key1")

    # Inspect the order arg passed to placeOrder
    ib_order_arg = fake_ib.placeOrder.call_args[0][1]
    # ib_insync.MarketOrder has totalQuantity but NOT lmtPrice
    assert ib_order_arg.totalQuantity == 10
    assert ib_order_arg.action == "BUY"
    assert ib_order_arg.orderType == "MKT"


@pytest.mark.asyncio
async def test_place_order_lmt_still_builds_limit_order():
    gateway = IBGateway(_stub_policy())
    fake_ib = MagicMock()
    fake_ib.placeOrder.return_value = MagicMock()
    gateway._ib = fake_ib
    gateway._account_id = "DU12345"

    order = PreparedOrder(action="BUY", quantity=10, order_type="LMT",
                          limit_price=5.55, tif="DAY")
    await gateway.place_order(_qualified_stk(), order, "key2")

    ib_order_arg = fake_ib.placeOrder.call_args[0][1]
    assert ib_order_arg.totalQuantity == 10
    assert ib_order_arg.lmtPrice == 5.55
    assert ib_order_arg.orderType == "LMT"


@pytest.mark.asyncio
async def test_place_order_mkt_rejects_when_limit_price_provided():
    """Defensive: if caller passes both order_type=MKT and a limit_price,
    fail loudly rather than silently picking one."""
    gateway = IBGateway(_stub_policy())
    fake_ib = MagicMock()
    gateway._ib = fake_ib
    gateway._account_id = "DU12345"

    order = PreparedOrder(action="BUY", quantity=10, order_type="MKT",
                          limit_price=5.55, tif="DAY")
    with pytest.raises(ValueError):
        await gateway.place_order(_qualified_stk(), order, "key3")
```

- [ ] **Step 3.2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/unit/test_gateway_market_order.py -v
```

Expected: 3 failures — gateway always builds LimitOrder.

- [ ] **Step 3.3: Make `PreparedOrder.limit_price` optional**

In `infra/ib/models.py`, replace the existing `PreparedOrder` (lines 60–66) with:

```python
@dataclass
class PreparedOrder:
    action: str         # 'BUY' | 'SELL'
    quantity: int
    order_type: str     # 'LMT' | 'MKT'
    tif: str            # 'DAY'
    limit_price: float | None = None  # required for LMT, must be None for MKT
```

(Note: moved `tif` ahead of `limit_price` so the optional field is last — required by dataclasses.)

- [ ] **Step 3.4: Update existing call sites that pass `limit_price` positionally**

Run the search:

```bash
grep -rn "PreparedOrder(" --include='*.py' /Users/jasonli/dev/trading-agent | grep -v __pycache__
```

You will find call sites in `skills/execution/order_submitter.py:64` and `skills/execution/price_walker.py:77`. Both pass arguments by keyword (`action=`, `quantity=`, `order_type=`, `limit_price=`, `tif=`), so reordering positional fields does not break them. Confirm by inspection — no edits needed.

- [ ] **Step 3.5: Branch `gateway.place_order` on `order_type`**

In `infra/ib/gateway.py`, replace the body of `place_order` (lines 319–348). The new body:

```python
    async def place_order(
        self,
        contract_ref: BrokerContractRef,
        order: PreparedOrder,
        client_order_id: str,
    ):
        self._assert_paper_guard()
        self._write_breaker.check()
        if not contract_ref.qualified:
            raise ValueError("contract_ref must be qualified before place_order")

        if order.order_type == "MKT" and order.limit_price is not None:
            raise ValueError("MKT order must not have limit_price")
        if order.order_type == "LMT" and order.limit_price is None:
            raise ValueError("LMT order requires limit_price")

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
                ib_order = LimitOrder(
                    action=order.action,
                    totalQuantity=order.quantity,
                    lmtPrice=order.limit_price,
                    tif=order.tif,
                    orderRef=client_order_id,
                )
            trade = self._ib.placeOrder(ib_contract, ib_order)
            self._write_breaker._record_success()
            logger.info("Placed %s order %s: %s x%s @ %s",
                        order.order_type, client_order_id, contract_ref.symbol,
                        order.quantity, order.limit_price if order.limit_price is not None else "MKT")
            return trade
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._write_breaker._record_failure()
            raise IBGatewayUnavailable(f"place_order failed: {exc}") from exc
```

- [ ] **Step 3.6: Run gateway tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_gateway_market_order.py -v
```

Expected: 3 passed.

- [ ] **Step 3.7: Run the full suite — confirm no regression**

```bash
.venv/bin/pytest -q
```

Expected: previously-passing tests still pass (especially `test_price_walker.py` and `test_order_submitter.py` which use LMT).

- [ ] **Step 3.8: Commit**

```bash
git add infra/ib/models.py infra/ib/gateway.py tests/unit/test_gateway_market_order.py
git commit -m "ib: support MKT in place_order; PreparedOrder.limit_price now optional"
```

---

## Task 4: TraderClassifier sizing changes

**Files:**
- Modify: `skills/signal/trader_classifier.py:15-20`, `:67-90` (shortcut), `:164-171` (LLM path)
- Modify: `tests/unit/test_trader_classifier.py` (update existing assertions)

- [ ] **Step 4.1: Update existing trader_classifier tests to assert NEW sizes**

In `tests/unit/test_trader_classifier.py`, change the size assertions:

- `test_shortcut_path_uses_stated_size_no_llm_call` (line 52): change `assert ctx.get("size_pct") == 0.02` → `assert ctx.get("size_pct") == 0.40` (stated 2% → bucket=LOW → size=0.40, NOT the stated value).
- `test_llm_path_high_confidence_high_bucket_fires_at_10pct`: rename to `..._fires_at_80pct`. Change `assert ctx.get("size_pct") == 0.10` → `assert ctx.get("size_pct") == 0.80`.
- `test_llm_path_mid_confidence_downgrades_to_low_5pct`: rename to `..._downgrades_to_low_40pct`. Change `assert ctx.get("size_pct") == 0.05` → `assert ctx.get("size_pct") == 0.40`.
- `test_stated_size_capped_at_10pct`: rename to `test_stated_size_above_threshold_buckets_high`. Change message to "Added 90% pos in XX" and assert `size_pct == 0.80`, `bucket == "HIGH"`.
- `test_shortcut_threshold_at_7_5_pct_buckets_high`: rename to `test_shortcut_threshold_at_60pct_buckets_high`. Change "Added 7.5% pos AAPL" → "Added 65% pos AAPL"; assert `size_pct == 0.80`, `bucket == "HIGH"`.

Also add this NEW test at the end of the file:

```python
@pytest.mark.asyncio
async def test_shortcut_below_threshold_buckets_low_size_40():
    profile = make_profile()
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "X", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "x"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "Added 30% pos AAPL",
    })

    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("size_pct") == 0.40
    assert ctx.get("bucket") == "LOW"
    assert ctx.get("size_source") == "shortcut_stated"
```

- [ ] **Step 4.2: Run trader_classifier tests to verify they fail**

```bash
.venv/bin/pytest tests/unit/test_trader_classifier.py -v
```

Expected: multiple failures (assertions on sizes don't match).

- [ ] **Step 4.3: Update sizing constants and shortcut path**

In `skills/signal/trader_classifier.py`, replace lines 15–20 with:

```python
HIGH_CONF_THRESHOLD = 0.80
DROP_CONF_THRESHOLD = 0.50
SIZE_LOW = 0.40
SIZE_HIGH = 0.80
MAX_STATED_SIZE = 0.80  # cap stated size at 80% (same as HIGH allocation)
SIZE_HIGH_SHORTCUT_THRESHOLD = 0.60  # stated >= 60% → bucket HIGH
```

Then in the same file, replace the shortcut block (lines 67–89) with:

```python
        # Deterministic shortcut: stated size + entry verb + exactly one ticker.
        # Stated size only chooses the bucket; actual size_pct is always
        # SIZE_LOW or SIZE_HIGH per the trim-ladder design.
        if (
            profile.prefer_message_size
            and features.stated_size_pct is not None
            and features.entry_verb_present
            and len(features.tickers_in_msg) == 1
        ):
            stated_pct = min(features.stated_size_pct / 100.0, MAX_STATED_SIZE)
            bucket = "HIGH" if stated_pct >= SIZE_HIGH_SHORTCUT_THRESHOLD else "LOW"
            size_pct = SIZE_HIGH if bucket == "HIGH" else SIZE_LOW
            updates = {
                "ticker": features.tickers_in_msg[0],
                "side": "long",
                "bucket": bucket,
                "confidence": 1.0,
                "size_pct": size_pct,
                "size_source": "shortcut_stated",
                "classifier_features_json": json.dumps(dataclasses.asdict(features)),
                "classifier_llm_response_json": None,
                "classifier_reason": "stated_size_in_message",
            }
            ctx.update(updates)
            return SkillResult(status="success", updates=updates)
```

The LLM path (lines 164–171) already reads `SIZE_LOW`/`SIZE_HIGH` constants — no change needed there; the constant updates flow through.

- [ ] **Step 4.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_trader_classifier.py -v
```

Expected: all tests pass with the new sizes.

- [ ] **Step 4.5: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 4.6: Commit**

```bash
git add skills/signal/trader_classifier.py tests/unit/test_trader_classifier.py
git commit -m "classifier: HIGH=80%/LOW=40% sizing; shortcut uses bucket-only size"
```

---

## Task 5: TradeIntentWriter writes equity

**Files:**
- Modify: `skills/execution/trade_intent_writer.py:45`
- Modify: `tests/unit/test_trade_intent_writer.py` (assertion update)

- [ ] **Step 5.1: Update existing test to expect "equity"**

In `tests/unit/test_trade_intent_writer.py`, in `test_creates_intent_row_and_sets_intent_id` (around line 27), add this assertion after the existing `assert record["channel"] == "mystic"`:

```python
    assert record["instrument_type"] == "equity"
```

- [ ] **Step 5.2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/unit/test_trade_intent_writer.py::test_creates_intent_row_and_sets_intent_id -v
```

Expected: FAIL — currently writes "option".

- [ ] **Step 5.3: Change the hardcoded value**

In `skills/execution/trade_intent_writer.py` line 45, change:

```python
            "instrument_type": "option",
```

to:

```python
            "instrument_type": "equity",
```

- [ ] **Step 5.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_trade_intent_writer.py -v
```

Expected: all pass.

- [ ] **Step 5.5: Commit**

```bash
git add skills/execution/trade_intent_writer.py tests/unit/test_trade_intent_writer.py
git commit -m "trade_intent_writer: hardcode instrument_type=equity (no options now)"
```

---

## Task 6: RthEntryGuard

**Files:**
- Create: `skills/execution/rth_entry_guard.py`
- Test: `tests/unit/test_rth_entry_guard.py`

- [ ] **Step 6.1: Write the failing tests**

Create `tests/unit/test_rth_entry_guard.py`:

```python
import pytest
from agent.context import Context
from skills.execution.rth_entry_guard import RthEntryGuard


@pytest.mark.asyncio
async def test_rth_session_passes():
    guard = RthEntryGuard()
    ctx = Context(trace_id="t", event_id="e", data={"execution_session": "rth"})
    result = await guard.run(ctx)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_premarket_session_skips():
    guard = RthEntryGuard()
    ctx = Context(trace_id="t", event_id="e", data={"execution_session": "premarket"})
    result = await guard.run(ctx)
    assert result.status == "skip"
    assert "entry_outside_rth" in (result.reason or "")


@pytest.mark.asyncio
async def test_afterhours_session_skips():
    guard = RthEntryGuard()
    ctx = Context(trace_id="t", event_id="e", data={"execution_session": "afterhours"})
    result = await guard.run(ctx)
    assert result.status == "skip"
    assert "entry_outside_rth" in (result.reason or "")


@pytest.mark.asyncio
async def test_missing_session_skips_defensively():
    """If upstream guard didn't set the session, fail-safe: skip."""
    guard = RthEntryGuard()
    ctx = Context(trace_id="t", event_id="e", data={})
    result = await guard.run(ctx)
    assert result.status == "skip"
```

- [ ] **Step 6.2: Run tests — expect import error**

```bash
.venv/bin/pytest tests/unit/test_rth_entry_guard.py -v
```

Expected: ImportError.

- [ ] **Step 6.3: Implement the guard**

Create `skills/execution/rth_entry_guard.py`:

```python
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class RthEntryGuard(Skill):
    """Drops entry signals received outside regular trading hours.

    The shares-only MKT entry path is RTH-only by design — premarket and
    afterhours liquidity is too thin for market orders. ExecutionEligibilityGuard
    sets `execution_session` upstream; this guard reads it and skips anything
    that isn't 'rth'.
    """

    name = "RthEntryGuard"

    async def run(self, ctx: Context) -> SkillResult:
        session = ctx.get("execution_session")
        if session == "rth":
            return SkillResult(status="success")
        return SkillResult(
            status="skip",
            reason=f"entry_outside_rth: session={session!r}",
        )
```

- [ ] **Step 6.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_rth_entry_guard.py -v
```

Expected: 4 passed.

- [ ] **Step 6.5: Commit**

```bash
git add skills/execution/rth_entry_guard.py tests/unit/test_rth_entry_guard.py
git commit -m "skill: RthEntryGuard drops non-RTH entries before MKT submit"
```

---

## Task 7: EquityContractBuilder

**Files:**
- Create: `skills/execution/equity_contract_builder.py`
- Test: `tests/unit/test_equity_contract_builder.py`

- [ ] **Step 7.1: Write the failing tests**

Create `tests/unit/test_equity_contract_builder.py`:

```python
import pytest
from unittest.mock import AsyncMock
from agent.context import Context
from infra.ib.models import BrokerContractRef
from skills.execution.equity_contract_builder import EquityContractBuilder


class FakeGateway:
    def __init__(self, qualified_ref):
        self._qualified = qualified_ref
        self.qualify = AsyncMock(return_value=qualified_ref)


@pytest.mark.asyncio
async def test_builds_qualified_stk_contract():
    qualified = BrokerContractRef(
        symbol="NVDA", sec_type="STK", exchange="SMART",
        currency="USD", qualified=True,
    )
    gw = FakeGateway(qualified)
    skill = EquityContractBuilder(gw)
    ctx = Context(trace_id="t", event_id="e", data={"ticker": "NVDA"})

    result = await skill.run(ctx)

    assert result.status == "success"
    selected = ctx.get("selected_contract")
    assert selected is qualified
    assert selected.sec_type == "STK"
    assert selected.qualified is True

    # Confirm we asked the gateway to qualify a STK contract
    raw = gw.qualify.call_args[0][0]
    assert raw.symbol == "NVDA"
    assert raw.sec_type == "STK"
    assert raw.exchange == "SMART"
    assert raw.currency == "USD"


@pytest.mark.asyncio
async def test_missing_ticker_fails():
    gw = FakeGateway(qualified_ref=None)
    skill = EquityContractBuilder(gw)
    ctx = Context(trace_id="t", event_id="e")
    result = await skill.run(ctx)
    assert result.status == "fail"
    assert "ticker" in (result.reason or "").lower()
```

- [ ] **Step 7.2: Run tests — expect import error**

```bash
.venv/bin/pytest tests/unit/test_equity_contract_builder.py -v
```

Expected: ImportError.

- [ ] **Step 7.3: Implement the builder**

Create `skills/execution/equity_contract_builder.py`:

```python
from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import BrokerContractRef

logger = logging.getLogger(__name__)


class EquityContractBuilder(Skill):
    """Builds and qualifies a STK contract for shares-only entry.

    Replaces ContractSelector in the new shares-only chain. One IB round-trip
    (qualify) per signal.
    """

    name = "EquityContractBuilder"

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        if not ticker:
            return SkillResult(status="fail", reason="equity_contract_builder: ticker missing")

        raw = BrokerContractRef(
            symbol=ticker,
            sec_type="STK",
            exchange="SMART",
            currency="USD",
        )
        qualified = await self._gateway.qualify(raw)
        logger.info("EquityContractBuilder: qualified STK %s", ticker)
        return SkillResult(status="success", updates={"selected_contract": qualified})
```

- [ ] **Step 7.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_equity_contract_builder.py -v
```

Expected: 2 passed.

- [ ] **Step 7.5: Commit**

```bash
git add skills/execution/equity_contract_builder.py tests/unit/test_equity_contract_builder.py
git commit -m "skill: EquityContractBuilder qualifies STK contract for shares entry"
```

---

## Task 8: TradeIntentStore.update_execution_state accepts fill_qty

**Files:**
- Modify: `infra/storage/trade_intent_store.py:32-92`
- Test: `tests/unit/test_trade_intent_store_fill_qty.py` (new)

- [ ] **Step 8.1: Write the failing test**

Create `tests/unit/test_trade_intent_store_fill_qty.py`:

```python
import pytest
import aiosqlite
from datetime import datetime, timezone
from infra.storage.db import SCHEMA
from infra.storage.trade_intent_store import TradeIntentStore


async def _make_intent(conn, intent_id="e1:NVDA:long"):
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """INSERT INTO trade_intents (
            intent_id, event_id, channel, ticker, side, instrument_type,
            conviction, policy_state, signal_received_at, intent_created_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (intent_id, "e1", "mystic", "NVDA", "long", "equity",
         "HIGH", "approved", now, now, now, now),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_update_execution_state_persists_fill_qty():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        await _make_intent(conn)

        store = TradeIntentStore(conn)
        await store.update_execution_state(
            "e1:NVDA:long",
            execution_state="filled",
            fill_price=145.32,
            fill_qty=550,
        )

        async with conn.execute(
            "SELECT fill_price, fill_qty FROM trade_intents WHERE intent_id=?",
            ("e1:NVDA:long",),
        ) as cur:
            row = await cur.fetchone()
        assert row["fill_price"] == 145.32
        assert row["fill_qty"] == 550
```

- [ ] **Step 8.2: Run test — expect FAIL**

```bash
.venv/bin/pytest tests/unit/test_trade_intent_store_fill_qty.py -v
```

Expected: FAIL — `update_execution_state` does not accept `fill_qty`.

- [ ] **Step 8.3: Add `fill_qty` parameter**

In `infra/storage/trade_intent_store.py`, modify `update_execution_state`. Add `fill_qty: int | None = None,` to the signature (right after `fill_price`). Then in the body, after the existing `if fill_price is not None: fields["fill_price"] = fill_price` lines, add:

```python
        if fill_qty is not None:
            fields["fill_qty"] = fill_qty
```

- [ ] **Step 8.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_trade_intent_store_fill_qty.py -v
```

Expected: PASS.

- [ ] **Step 8.5: Commit**

```bash
git add infra/storage/trade_intent_store.py tests/unit/test_trade_intent_store_fill_qty.py
git commit -m "trade_intent_store: update_execution_state accepts fill_qty"
```

---

## Task 9: TrimLadderStore

**Files:**
- Create: `infra/storage/trim_ladder_store.py`
- Test: `tests/unit/test_trim_ladder_store.py`

- [ ] **Step 9.1: Write the failing tests**

Create `tests/unit/test_trim_ladder_store.py`:

```python
import pytest
import aiosqlite
from datetime import datetime, timezone
from infra.storage.db import SCHEMA
from infra.storage.trim_ladder_store import TrimLadderStore, TrimRungRow


async def _make_intent(conn, intent_id, ticker="NVDA",
                       fill_price=100.0, fill_qty=10, state="filled"):
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """INSERT INTO trade_intents (
            intent_id, event_id, channel, ticker, side, instrument_type,
            conviction, policy_state, signal_received_at, intent_created_at,
            created_at, updated_at, execution_state, fill_price, fill_qty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (intent_id, "e1", "mystic", ticker, "long", "equity",
         "HIGH", "approved", now, now, now, now, state, fill_price, fill_qty),
    )
    await conn.commit()


@pytest.fixture
async def conn():
    async with aiosqlite.connect(":memory:") as c:
        c.row_factory = aiosqlite.Row
        await c.executescript(SCHEMA)
        await c.commit()
        yield c


@pytest.mark.asyncio
async def test_arm_inserts_rungs(conn):
    await _make_intent(conn, "e1:NVDA:long")
    store = TrimLadderStore(conn)
    await store.arm(
        intent_id="e1:NVDA:long",
        rungs=[(1, 0.05, 0.40), (2, 0.10, 0.40)],
    )

    async with conn.execute(
        "SELECT rung, threshold_pct, trim_pct, fired_at FROM trade_intent_trims "
        "WHERE intent_id=? ORDER BY rung", ("e1:NVDA:long",),
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["rung"] == 1
    assert rows[0]["threshold_pct"] == 0.05
    assert rows[0]["trim_pct"] == 0.40
    assert rows[0]["fired_at"] is None
    assert rows[1]["rung"] == 2


@pytest.mark.asyncio
async def test_get_unfired_rungs_joins_position_data(conn):
    await _make_intent(conn, "e1:NVDA:long", ticker="NVDA",
                       fill_price=145.0, fill_qty=550)
    await _make_intent(conn, "e2:AAPL:long", ticker="AAPL",
                       fill_price=180.0, fill_qty=200)
    store = TrimLadderStore(conn)
    await store.arm("e1:NVDA:long", [(1, 0.05, 0.40), (2, 0.10, 0.40)])
    await store.arm("e2:AAPL:long", [(1, 0.05, 0.40), (2, 0.10, 0.40)])

    rows = await store.get_unfired_rungs()
    by_intent = {(r.intent_id, r.rung): r for r in rows}
    assert (("e1:NVDA:long", 1)) in by_intent
    assert by_intent["e1:NVDA:long", 1].ticker == "NVDA"
    assert by_intent["e1:NVDA:long", 1].fill_price == 145.0
    assert by_intent["e1:NVDA:long", 1].fill_qty == 550
    assert by_intent["e2:AAPL:long", 2].fill_price == 180.0


@pytest.mark.asyncio
async def test_get_unfired_excludes_fired(conn):
    await _make_intent(conn, "e1:NVDA:long")
    store = TrimLadderStore(conn)
    await store.arm("e1:NVDA:long", [(1, 0.05, 0.40), (2, 0.10, 0.40)])
    await store.mark_fired(
        intent_id="e1:NVDA:long",
        rung=1,
        fire_price=105.0,
        sold_qty=4,
        sold_avg_price=104.95,
        broker_order_ref="IB-1",
    )

    rows = await store.get_unfired_rungs()
    rungs_for_e1 = [r for r in rows if r.intent_id == "e1:NVDA:long"]
    assert len(rungs_for_e1) == 1
    assert rungs_for_e1[0].rung == 2


@pytest.mark.asyncio
async def test_get_unfired_excludes_unfilled_intents(conn):
    """If the entry never filled, no trims should fire."""
    await _make_intent(conn, "e3:TSLA:long", state="cancelled_unfilled")
    store = TrimLadderStore(conn)
    await store.arm("e3:TSLA:long", [(1, 0.05, 0.40)])
    rows = await store.get_unfired_rungs()
    assert all(r.intent_id != "e3:TSLA:long" for r in rows)


@pytest.mark.asyncio
async def test_mark_fired_persists_all_fields(conn):
    await _make_intent(conn, "e1:NVDA:long")
    store = TrimLadderStore(conn)
    await store.arm("e1:NVDA:long", [(1, 0.05, 0.40)])
    await store.mark_fired(
        intent_id="e1:NVDA:long", rung=1,
        fire_price=152.0, sold_qty=4, sold_avg_price=151.95,
        broker_order_ref="IB-99",
    )

    async with conn.execute(
        "SELECT fire_price, sold_qty, sold_avg_price, broker_order_ref, fired_at "
        "FROM trade_intent_trims WHERE intent_id=? AND rung=?",
        ("e1:NVDA:long", 1),
    ) as cur:
        row = await cur.fetchone()
    assert row["fire_price"] == 152.0
    assert row["sold_qty"] == 4
    assert row["sold_avg_price"] == 151.95
    assert row["broker_order_ref"] == "IB-99"
    assert row["fired_at"] is not None
```

- [ ] **Step 9.2: Run tests — expect ImportError**

```bash
.venv/bin/pytest tests/unit/test_trim_ladder_store.py -v
```

Expected: ImportError.

- [ ] **Step 9.3: Implement the store**

Create `infra/storage/trim_ladder_store.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import aiosqlite


@dataclass
class TrimRungRow:
    intent_id: str
    rung: int
    threshold_pct: float
    trim_pct: float
    ticker: str
    fill_price: float
    fill_qty: int


class TrimLadderStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def arm(
        self,
        intent_id: str,
        rungs: list[tuple[int, float, float]],
    ) -> None:
        """Insert one row per rung (rung_idx, threshold_pct, trim_pct)."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.executemany(
            """INSERT OR IGNORE INTO trade_intent_trims
               (intent_id, rung, threshold_pct, trim_pct, armed_at)
               VALUES (?, ?, ?, ?, ?)""",
            [(intent_id, r[0], r[1], r[2], now) for r in rungs],
        )
        await self._conn.commit()

    async def get_unfired_rungs(self) -> list[TrimRungRow]:
        async with self._conn.execute(
            """SELECT t.intent_id, t.rung, t.threshold_pct, t.trim_pct,
                      ti.ticker, ti.fill_price, ti.fill_qty
               FROM trade_intent_trims t
               JOIN trade_intents ti ON t.intent_id = ti.intent_id
               WHERE t.fired_at IS NULL
                 AND ti.execution_state = 'filled'
                 AND ti.fill_price IS NOT NULL
                 AND ti.fill_qty IS NOT NULL""",
        ) as cur:
            rows = await cur.fetchall()
        return [
            TrimRungRow(
                intent_id=r["intent_id"],
                rung=r["rung"],
                threshold_pct=r["threshold_pct"],
                trim_pct=r["trim_pct"],
                ticker=r["ticker"],
                fill_price=r["fill_price"],
                fill_qty=r["fill_qty"],
            )
            for r in rows
        ]

    async def mark_fired(
        self,
        *,
        intent_id: str,
        rung: int,
        fire_price: float,
        sold_qty: int,
        sold_avg_price: float | None,
        broker_order_ref: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """UPDATE trade_intent_trims
               SET fired_at=?, fire_price=?, sold_qty=?, sold_avg_price=?, broker_order_ref=?
               WHERE intent_id=? AND rung=?""",
            (now, fire_price, sold_qty, sold_avg_price, broker_order_ref, intent_id, rung),
        )
        await self._conn.commit()
```

- [ ] **Step 9.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_trim_ladder_store.py -v
```

Expected: 5 passed.

- [ ] **Step 9.5: Commit**

```bash
git add infra/storage/trim_ladder_store.py tests/unit/test_trim_ladder_store.py
git commit -m "store: TrimLadderStore for arming and firing trim rungs"
```

---

## Task 10: SharesMarketSubmitter

**Files:**
- Create: `skills/execution/shares_market_submitter.py`
- Test: `tests/unit/test_shares_market_submitter.py`

- [ ] **Step 10.1: Write the failing tests**

Create `tests/unit/test_shares_market_submitter.py`:

```python
import pytest
import aiosqlite
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock
from agent.context import Context
from infra.storage.db import SCHEMA
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from infra.ib.models import BrokerContractRef, FillResult, FillStatus
from skills.execution.shares_market_submitter import SharesMarketSubmitter


def _policy():
    p = MagicMock()
    p.execution.fill_wait_timeout_seconds = 30.0
    p.execution.trim_ladder.rungs = [
        MagicMock(threshold_pct=0.05, trim_pct=0.40),
        MagicMock(threshold_pct=0.10, trim_pct=0.40),
    ]
    return p


def _qualified_stk(symbol="NVDA"):
    return BrokerContractRef(
        symbol=symbol, sec_type="STK", exchange="SMART",
        currency="USD", qualified=True,
    )


def _gateway(fill_qty=550, avg=145.32):
    fake_trade = MagicMock()
    fake_trade.order.orderId = "IB-1"
    fake_fill = FillResult(
        status=FillStatus.FILLED,
        broker_order_id="IB-1",
        perm_id=42,
        submitted_qty=fill_qty,
        filled_qty=fill_qty,
        remaining_qty=0,
        avg_fill_price=avg,
        last_status="Filled",
        status_timestamp="2026-05-04T15:00:00+00:00",
    )
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.wait_fill = AsyncMock(return_value=fake_fill)
    return gw, fake_trade, fake_fill


async def _make_intent(conn, intent_id="evt1:NVDA:long", ticker="NVDA"):
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """INSERT INTO trade_intents (
            intent_id, event_id, channel, ticker, side, instrument_type,
            conviction, policy_state, signal_received_at, intent_created_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (intent_id, "evt1", "mystic", ticker, "long", "equity",
         "HIGH", "approved", now, now, now, now),
    )
    await conn.commit()


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as c:
        c.row_factory = aiosqlite.Row
        await c.executescript(SCHEMA)
        await c.commit()
        yield c


def _ctx(intent_id="evt1:NVDA:long", quantity=550, side="long"):
    ctx = Context(trace_id="t", event_id="evt1", data={
        "intent_id": intent_id,
        "ticker": "NVDA",
        "selected_contract": _qualified_stk(),
        "quantity": quantity,
        "side": side,
    })
    return ctx


@pytest.mark.asyncio
async def test_long_signal_places_mkt_order_and_arms_rungs(db):
    await _make_intent(db)
    gw, _, _ = _gateway(fill_qty=550, avg=145.32)
    intent_store = TradeIntentStore(db)
    trim_store = TrimLadderStore(db)
    skill = SharesMarketSubmitter(_policy(), gw, intent_store, trim_store)

    result = await skill.run(_ctx())

    assert result.status == "success"

    # Order built as MKT, BUY
    submitted = gw.place_order.call_args[0][1]
    assert submitted.order_type == "MKT"
    assert submitted.action == "BUY"
    assert submitted.quantity == 550
    assert submitted.limit_price is None

    # Intent updated with fill
    async with db.execute(
        "SELECT fill_price, fill_qty, execution_state FROM trade_intents WHERE intent_id=?",
        ("evt1:NVDA:long",),
    ) as cur:
        row = await cur.fetchone()
    assert row["fill_price"] == 145.32
    assert row["fill_qty"] == 550
    assert row["execution_state"] == "filled"

    # Trim rungs armed
    async with db.execute(
        "SELECT rung, threshold_pct, trim_pct, fired_at FROM trade_intent_trims "
        "WHERE intent_id=? ORDER BY rung", ("evt1:NVDA:long",),
    ) as cur:
        trims = await cur.fetchall()
    assert len(trims) == 2
    assert trims[0]["rung"] == 1 and trims[0]["threshold_pct"] == 0.05 and trims[0]["trim_pct"] == 0.40
    assert trims[1]["rung"] == 2 and trims[1]["threshold_pct"] == 0.10 and trims[1]["trim_pct"] == 0.40
    assert all(t["fired_at"] is None for t in trims)


@pytest.mark.asyncio
async def test_short_signal_skipped_no_order_no_trims(db):
    await _make_intent(db, intent_id="evt2:NVDA:short")
    gw, _, _ = _gateway()
    intent_store = TradeIntentStore(db)
    trim_store = TrimLadderStore(db)
    skill = SharesMarketSubmitter(_policy(), gw, intent_store, trim_store)

    ctx = _ctx(intent_id="evt2:NVDA:short", side="short")
    result = await skill.run(ctx)

    assert result.status == "skip"
    assert "unsupported_short_signal" in (result.reason or "")
    gw.place_order.assert_not_called()

    # No trim rows created
    async with db.execute(
        "SELECT COUNT(*) AS n FROM trade_intent_trims WHERE intent_id=?",
        ("evt2:NVDA:short",),
    ) as cur:
        count = (await cur.fetchone())["n"]
    assert count == 0


@pytest.mark.asyncio
async def test_partial_fill_status_arms_trims_for_partial_qty(db):
    """IB returns PARTIAL_FILL with filled_qty < submitted; we accept it."""
    await _make_intent(db)
    gw, _, _ = _gateway()
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.PARTIAL_FILL, broker_order_id="IB-1", perm_id=42,
        submitted_qty=550, filled_qty=300, remaining_qty=250,
        avg_fill_price=145.50, last_status="PartiallyFilled",
        status_timestamp="2026-05-04T15:00:00+00:00",
    ))
    intent_store = TradeIntentStore(db)
    trim_store = TrimLadderStore(db)
    skill = SharesMarketSubmitter(_policy(), gw, intent_store, trim_store)

    result = await skill.run(_ctx(quantity=550))
    assert result.status == "success"

    async with db.execute(
        "SELECT fill_qty FROM trade_intents WHERE intent_id=?",
        ("evt1:NVDA:long",),
    ) as cur:
        row = await cur.fetchone()
    assert row["fill_qty"] == 300

    # Trim rungs armed against the partial qty
    async with db.execute(
        "SELECT COUNT(*) AS n FROM trade_intent_trims WHERE intent_id=?",
        ("evt1:NVDA:long",),
    ) as cur:
        count = (await cur.fetchone())["n"]
    assert count == 2


@pytest.mark.asyncio
async def test_zero_fill_does_not_arm_trims(db):
    await _make_intent(db)
    gw, _, _ = _gateway()
    # Override wait_fill to return zero filled
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.CANCELLED, broker_order_id="IB-1", perm_id=None,
        submitted_qty=550, filled_qty=0, remaining_qty=550,
        avg_fill_price=None, last_status="Cancelled",
        status_timestamp="2026-05-04T15:00:00+00:00",
    ))
    intent_store = TradeIntentStore(db)
    trim_store = TrimLadderStore(db)
    skill = SharesMarketSubmitter(_policy(), gw, intent_store, trim_store)

    result = await skill.run(_ctx())
    assert result.status == "fail"

    async with db.execute(
        "SELECT COUNT(*) AS n FROM trade_intent_trims WHERE intent_id=?",
        ("evt1:NVDA:long",),
    ) as cur:
        count = (await cur.fetchone())["n"]
    assert count == 0
```

- [ ] **Step 10.2: Run tests — expect ImportError**

```bash
.venv/bin/pytest tests/unit/test_shares_market_submitter.py -v
```

Expected: ImportError.

- [ ] **Step 10.3: Implement the submitter**

Create `skills/execution/shares_market_submitter.py`:

```python
from __future__ import annotations
import logging
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import PreparedOrder, FillStatus

logger = logging.getLogger(__name__)


class SharesMarketSubmitter(Skill):
    """Submits one MKT BUY in shares, waits for fill, then arms trim rungs.

    Drops short-side signals (the trim ladder is upward-only by design;
    shorts will need their own spec).
    """

    name = "SharesMarketSubmitter"

    def __init__(self, policy, gateway, trade_intent_store, trim_ladder_store) -> None:
        self._policy = policy
        self._gateway = gateway
        self._intent_store = trade_intent_store
        self._trim_store = trim_ladder_store

    async def run(self, ctx: Context) -> SkillResult:
        side = ctx.get("side")
        if side != "long":
            return SkillResult(
                status="skip",
                reason=f"unsupported_short_signal: side={side!r}",
            )

        intent_id = ctx.get("intent_id")
        contract_ref = ctx.get("selected_contract")
        quantity = ctx.get("quantity")
        if not all([intent_id, contract_ref, quantity]):
            return SkillResult(status="fail", reason="shares_market_submitter: missing intent_id/contract/quantity")

        order = PreparedOrder(
            action="BUY",
            quantity=quantity,
            order_type="MKT",
            tif="DAY",
            limit_price=None,
        )
        idempotency_key = f"{ctx.trace_id}:SharesMarketSubmitter:{ctx.event_id}"
        submitted_at = datetime.now(timezone.utc).isoformat()

        try:
            trade = await self._gateway.place_order(contract_ref, order, idempotency_key)
        except IBGatewayUnavailable as exc:
            await self._intent_store.update_execution_state(
                intent_id, execution_state="failed", dlq_reason=f"broker_unavailable: {exc}",
            )
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        ack_at = datetime.now(timezone.utc).isoformat()
        broker_order_ref = str(trade.order.orderId)
        await self._intent_store.update_execution_state(
            intent_id,
            execution_state="submitted",
            outbox_status="dispatched",
            order_submitted_at=submitted_at,
            order_ack_at=ack_at,
            broker_order_ref=broker_order_ref,
        )

        fill = await self._gateway.wait_fill(
            trade, timeout=self._policy.execution.fill_wait_timeout_seconds,
        )

        # Spec: partial fill is acceptable — arm trims for the actually-filled qty.
        # Only treat zero-fill or hard-cancel as failure.
        accepted = (
            fill.filled_qty > 0
            and fill.status in (FillStatus.FILLED, FillStatus.PARTIAL_FILL)
        )
        if not accepted:
            await self._intent_store.update_execution_state(
                intent_id,
                execution_state="cancelled_unfilled",
                cancel_reason=f"fill_status={fill.status.value} filled={fill.filled_qty}",
                cancelled_at=datetime.now(timezone.utc).isoformat(),
            )
            return SkillResult(
                status="fail",
                reason=f"entry_unfilled: status={fill.status.value} filled_qty={fill.filled_qty}",
            )

        filled_at = datetime.now(timezone.utc).isoformat()
        await self._intent_store.update_execution_state(
            intent_id,
            execution_state="filled",
            outbox_status="confirmed",
            fill_price=fill.avg_fill_price,
            fill_qty=fill.filled_qty,
            filled_at=filled_at,
        )

        rung_specs = [
            (idx + 1, r.threshold_pct, r.trim_pct)
            for idx, r in enumerate(self._policy.execution.trim_ladder.rungs)
        ]
        await self._trim_store.arm(intent_id=intent_id, rungs=rung_specs)

        logger.info("SharesMarketSubmitter: filled %s qty=%d @ %.4f, armed %d trim rungs",
                    ctx.get("ticker"), fill.filled_qty, fill.avg_fill_price, len(rung_specs))
        return SkillResult(status="success", updates={
            "fill_status": "filled",
            "fill_price": fill.avg_fill_price,
            "filled_qty": fill.filled_qty,
            "avg_fill_price": fill.avg_fill_price,
        })
```

- [ ] **Step 10.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_shares_market_submitter.py -v
```

Expected: 4 passed.

- [ ] **Step 10.5: Commit**

```bash
git add skills/execution/shares_market_submitter.py tests/unit/test_shares_market_submitter.py
git commit -m "skill: SharesMarketSubmitter does MKT BUY + arms trim rungs"
```

---

## Task 11: ExitLadder background task

**Files:**
- Create: `agent/exit_ladder.py`
- Test: `tests/unit/test_exit_ladder.py`

- [ ] **Step 11.1: Write the failing tests**

Create `tests/unit/test_exit_ladder.py`:

```python
import asyncio
import pytest
import aiosqlite
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock
from agent.exit_ladder import ExitLadder
from infra.storage.db import SCHEMA
from infra.storage.trim_ladder_store import TrimLadderStore
from infra.ib.models import BrokerContractRef, FillResult, FillStatus


async def _make_filled_intent(conn, intent_id, ticker, fill_price, fill_qty):
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """INSERT INTO trade_intents (
            intent_id, event_id, channel, ticker, side, instrument_type,
            conviction, policy_state, signal_received_at, intent_created_at,
            created_at, updated_at, execution_state, fill_price, fill_qty
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (intent_id, "e", "mystic", ticker, "long", "equity",
         "HIGH", "approved", now, now, now, now, "filled", fill_price, fill_qty),
    )
    await conn.commit()


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as c:
        c.row_factory = aiosqlite.Row
        await c.executescript(SCHEMA)
        await c.commit()
        yield c


def _gw(quote_sequence: list[float]):
    """Gateway whose get_quote returns one element from the sequence per call,
    repeating the last value once exhausted."""
    quotes = list(quote_sequence)
    gw = MagicMock()
    async def _get_quote(ticker):
        return quotes.pop(0) if quotes else quote_sequence[-1]
    gw.get_quote = _get_quote

    qualified = BrokerContractRef(symbol="NVDA", sec_type="STK", exchange="SMART",
                                  currency="USD", qualified=True)
    gw.qualify = AsyncMock(return_value=qualified)

    fake_trade = MagicMock()
    fake_trade.order.orderId = "IB-SELL-1"
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="IB-SELL-1", perm_id=1,
        submitted_qty=4, filled_qty=4, remaining_qty=0,
        avg_fill_price=None, last_status="Filled",
        status_timestamp="2026-05-04T15:00:00+00:00",
    ))
    return gw


def _is_rth_true():
    return True


@pytest.mark.asyncio
async def test_no_fire_when_quote_below_thresholds(db):
    await _make_filled_intent(db, "e1:NVDA:long", "NVDA", 100.0, 10)
    trim = TrimLadderStore(db)
    await trim.arm("e1:NVDA:long", [(1, 0.05, 0.40), (2, 0.10, 0.40)])

    gw = _gw([102.0])
    ladder = ExitLadder(_policy(), gw, trim, is_rth=_is_rth_true)

    await ladder.tick_once()

    gw.place_order.assert_not_called()
    rows = await trim.get_unfired_rungs()
    assert len(rows) == 2  # both still unfired


@pytest.mark.asyncio
async def test_fire_r1_when_quote_crosses_5pct(db):
    await _make_filled_intent(db, "e1:NVDA:long", "NVDA", 100.0, 10)
    trim = TrimLadderStore(db)
    await trim.arm("e1:NVDA:long", [(1, 0.05, 0.40), (2, 0.10, 0.40)])

    gw = _gw([105.5])  # above +5%, below +10%
    ladder = ExitLadder(_policy(), gw, trim, is_rth=_is_rth_true)

    await ladder.tick_once()

    # Exactly one sell submitted, R1
    assert gw.place_order.call_count == 1
    submitted = gw.place_order.call_args[0][1]
    assert submitted.order_type == "MKT"
    assert submitted.action == "SELL"
    assert submitted.quantity == 4  # round(10 * 0.40)

    rows = await trim.get_unfired_rungs()
    rungs_for_e1 = sorted(r.rung for r in rows if r.intent_id == "e1:NVDA:long")
    assert rungs_for_e1 == [2]  # R1 fired, R2 still armed


@pytest.mark.asyncio
async def test_gap_up_above_10pct_fires_both_in_one_tick(db):
    await _make_filled_intent(db, "e1:NVDA:long", "NVDA", 100.0, 10)
    trim = TrimLadderStore(db)
    await trim.arm("e1:NVDA:long", [(1, 0.05, 0.40), (2, 0.10, 0.40)])

    gw = _gw([112.0, 112.0])  # past +10%; gw will return 112 for both calls
    ladder = ExitLadder(_policy(), gw, trim, is_rth=_is_rth_true)

    await ladder.tick_once()

    assert gw.place_order.call_count == 2
    rows = await trim.get_unfired_rungs()
    assert all(r.intent_id != "e1:NVDA:long" for r in rows)


@pytest.mark.asyncio
async def test_does_not_fire_outside_rth(db):
    await _make_filled_intent(db, "e1:NVDA:long", "NVDA", 100.0, 10)
    trim = TrimLadderStore(db)
    await trim.arm("e1:NVDA:long", [(1, 0.05, 0.40), (2, 0.10, 0.40)])

    gw = _gw([200.0])
    ladder = ExitLadder(_policy(), gw, trim, is_rth=lambda: False)

    await ladder.tick_once()

    gw.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_quote_failure_skips_position_does_not_crash(db):
    await _make_filled_intent(db, "e1:NVDA:long", "NVDA", 100.0, 10)
    trim = TrimLadderStore(db)
    await trim.arm("e1:NVDA:long", [(1, 0.05, 0.40)])

    gw = _gw([])
    async def _exploding_quote(ticker):
        raise RuntimeError("IB connection lost")
    gw.get_quote = _exploding_quote

    ladder = ExitLadder(_policy(), gw, trim, is_rth=_is_rth_true)
    # Must not raise
    await ladder.tick_once()

    gw.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_rounding_minimum_one_share(db):
    """Position of 1 share: trim_pct=0.40 → round(0.40)=0; clamp to 1."""
    await _make_filled_intent(db, "e1:NVDA:long", "NVDA", 100.0, 1)
    trim = TrimLadderStore(db)
    await trim.arm("e1:NVDA:long", [(1, 0.05, 0.40)])

    gw = _gw([106.0])
    ladder = ExitLadder(_policy(), gw, trim, is_rth=_is_rth_true)
    await ladder.tick_once()

    submitted = gw.place_order.call_args[0][1]
    assert submitted.quantity == 1


@pytest.mark.asyncio
async def test_loop_start_stop():
    """ExitLadder.start() launches a task; stop() cancels it cleanly."""
    gw = _gw([100.0])
    trim = MagicMock()
    trim.get_unfired_rungs = AsyncMock(return_value=[])
    ladder = ExitLadder(_policy(interval_seconds=0.05), gw, trim, is_rth=_is_rth_true)

    ladder.start()
    await asyncio.sleep(0.15)  # allow ~3 ticks
    await ladder.stop()
    assert trim.get_unfired_rungs.await_count >= 1


def _policy(interval_seconds=2):
    p = MagicMock()
    p.execution.exit_poll_interval_seconds = interval_seconds
    return p
```

- [ ] **Step 11.2: Run tests — expect ImportError**

```bash
.venv/bin/pytest tests/unit/test_exit_ladder.py -v
```

Expected: ImportError.

- [ ] **Step 11.3: Implement the ladder**

Create `agent/exit_ladder.py`:

```python
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from infra.ib.models import BrokerContractRef, PreparedOrder, FillStatus

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_RTH_START = dtime(9, 30)
_RTH_END = dtime(16, 0)


def _default_is_rth() -> bool:
    now_et = datetime.now(_ET).time()
    return _RTH_START <= now_et < _RTH_END


def _round_trim_qty(original_qty: int, trim_pct: float) -> int:
    """round_half_up with floor of 1, capped at original_qty."""
    raw = original_qty * trim_pct
    rounded = int(raw + 0.5)  # half-up
    return max(1, min(rounded, original_qty))


class ExitLadder:
    """Background task that polls quotes for unfired trim rungs and fires
    MKT sell orders when the threshold is crossed.

    Single-task; processes positions sequentially per tick. RTH-only.
    Quote failures are logged and the position is skipped for that tick.
    Sell-order failures mark the rung fired with no broker_order_ref so we
    do not retry (avoiding sell-side retry storms).
    """

    def __init__(
        self,
        policy,
        gateway,
        trim_ladder_store,
        is_rth=None,
    ) -> None:
        self._policy = policy
        self._gateway = gateway
        self._store = trim_ladder_store
        self._is_rth = is_rth or _default_is_rth
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="exit_ladder")
        logger.info("ExitLadder: started (interval=%ss)",
                    self._policy.execution.exit_poll_interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("ExitLadder: stopped")

    async def _loop(self) -> None:
        interval = self._policy.execution.exit_poll_interval_seconds
        while not self._stopped.is_set():
            try:
                await self.tick_once()
            except Exception:
                logger.exception("ExitLadder: tick failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def tick_once(self) -> None:
        if not self._is_rth():
            return
        rungs = await self._store.get_unfired_rungs()
        if not rungs:
            return

        # Group by ticker to share one quote round-trip per ticker per tick.
        by_ticker: dict[str, list] = {}
        for r in rungs:
            by_ticker.setdefault(r.ticker, []).append(r)

        for ticker, ticker_rungs in by_ticker.items():
            try:
                last = await self._gateway.get_quote(ticker)
            except Exception as exc:
                logger.warning("ExitLadder: get_quote(%s) failed: %s; skipping tick", ticker, exc)
                continue

            # Fire eligible rungs in rung order so R1 always fires before R2
            ticker_rungs.sort(key=lambda r: r.rung)
            for r in ticker_rungs:
                threshold_price = r.fill_price * (1.0 + r.threshold_pct)
                if last < threshold_price:
                    continue
                await self._fire(r, last)

    async def _fire(self, rung_row, fire_price: float) -> None:
        trim_qty = _round_trim_qty(rung_row.fill_qty, rung_row.trim_pct)

        contract = BrokerContractRef(
            symbol=rung_row.ticker, sec_type="STK", exchange="SMART",
            currency="USD",
        )
        qualified = await self._gateway.qualify(contract)

        order = PreparedOrder(
            action="SELL", quantity=trim_qty, order_type="MKT",
            tif="DAY", limit_price=None,
        )
        idempotency_key = f"{rung_row.intent_id}:trim:R{rung_row.rung}"

        try:
            trade = await self._gateway.place_order(qualified, order, idempotency_key)
        except Exception as exc:
            logger.error("ExitLadder: trim sell failed for %s R%d: %s",
                         rung_row.intent_id, rung_row.rung, exc)
            await self._store.mark_fired(
                intent_id=rung_row.intent_id, rung=rung_row.rung,
                fire_price=fire_price, sold_qty=0, sold_avg_price=None,
                broker_order_ref=None,
            )
            return

        broker_ref = str(trade.order.orderId)
        # Wait for fill to capture avg price; if it never fills we still mark
        # the rung fired (we never want sell-side retry storms).
        fill = await self._gateway.wait_fill(trade, timeout=10.0)
        sold_qty = fill.filled_qty if fill.status == FillStatus.FILLED else 0
        sold_avg = fill.avg_fill_price if fill.status == FillStatus.FILLED else None

        await self._store.mark_fired(
            intent_id=rung_row.intent_id, rung=rung_row.rung,
            fire_price=fire_price, sold_qty=sold_qty,
            sold_avg_price=sold_avg, broker_order_ref=broker_ref,
        )
        logger.info("ExitLadder: fired %s R%d at %.4f, sold %d/%d shares",
                    rung_row.intent_id, rung_row.rung, fire_price, sold_qty, trim_qty)
```

- [ ] **Step 11.4: Run tests — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_exit_ladder.py -v
```

Expected: 7 passed.

- [ ] **Step 11.5: Commit**

```bash
git add agent/exit_ladder.py tests/unit/test_exit_ladder.py
git commit -m "agent: ExitLadder background task fires MKT trims on +5/+10% moves"
```

---

## Task 12: Wire chain in registry + main

**Files:**
- Modify: `agent/registry.py:44-73` (`build_phase2b_execution_chain`)
- Modify: `main.py:48-154` (start/stop ExitLadder)
- Test: `tests/unit/test_phase2b_chain_composition.py` (new)

- [ ] **Step 12.1: Write the chain composition test**

Create `tests/unit/test_phase2b_chain_composition.py`:

```python
from unittest.mock import MagicMock
from agent.registry import build_phase2b_execution_chain


def _stub_policy():
    p = MagicMock()
    p.execution.fill_wait_timeout_seconds = 30.0
    p.execution.trim_ladder.rungs = []
    return p


def test_chain_excludes_option_skills():
    chain = build_phase2b_execution_chain(
        _stub_policy(),
        execution_store=MagicMock(),
        gateway=MagicMock(),
        trade_intent_store=MagicMock(),
        trim_ladder_store=MagicMock(),
    )
    names = [s.name for s in chain]
    for forbidden in ("ChainLookup", "InstrumentMarketabilityGuard",
                      "ContractSelector", "OrderPricer", "PriceWalker"):
        assert forbidden not in names, f"{forbidden} must not appear in shares-only chain"


def test_chain_includes_new_shares_skills_in_order():
    chain = build_phase2b_execution_chain(
        _stub_policy(),
        execution_store=MagicMock(),
        gateway=MagicMock(),
        trade_intent_store=MagicMock(),
        trim_ladder_store=MagicMock(),
    )
    names = [s.name for s in chain]
    expected = [
        "TradeIntentWriter", "ChannelPolicyGuard", "CooldownGuard",
        "ExecutionEligibilityGuard", "RthEntryGuard",
        "EquityContractBuilder", "OrderSizer", "SharesMarketSubmitter",
    ]
    assert names == expected
```

- [ ] **Step 12.2: Run test — expect FAIL**

```bash
.venv/bin/pytest tests/unit/test_phase2b_chain_composition.py -v
```

Expected: FAIL — chain still uses option skills, `trim_ladder_store` arg unknown.

- [ ] **Step 12.3: Rewire `build_phase2b_execution_chain`**

In `agent/registry.py`, replace the entire `build_phase2b_execution_chain` function (lines 44–73) with:

```python
def build_phase2b_execution_chain(policy, execution_store, gateway,
                                   trade_intent_store=None,
                                   trim_ladder_store=None) -> list:
    from skills.execution.trade_intent_writer import TradeIntentWriter
    from skills.execution.channel_policy_guard import ChannelPolicyGuard
    from skills.execution.cooldown_guard import CooldownGuard
    from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
    from skills.execution.rth_entry_guard import RthEntryGuard
    from skills.execution.equity_contract_builder import EquityContractBuilder
    from skills.execution.order_sizer import OrderSizer
    from skills.execution.shares_market_submitter import SharesMarketSubmitter

    if trade_intent_store is None:
        raise ValueError("trade_intent_store is required for shares-only chain")
    if trim_ladder_store is None:
        raise ValueError("trim_ladder_store is required for shares-only chain")

    return [
        TradeIntentWriter(trade_intent_store),
        ChannelPolicyGuard(policy, trade_intent_store),
        CooldownGuard(policy, trade_intent_store),
        ExecutionEligibilityGuard(policy),
        RthEntryGuard(),
        EquityContractBuilder(gateway),
        OrderSizer(gateway),
        SharesMarketSubmitter(policy, gateway, trade_intent_store, trim_ladder_store),
    ]
```

- [ ] **Step 12.4: Run chain test — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_phase2b_chain_composition.py -v
```

Expected: 2 passed.

- [ ] **Step 12.5: Wire `TrimLadderStore` and `ExitLadder` into `main.py`**

In `main.py`, add this import near the other storage imports (around line 30):

```python
from infra.storage.trim_ladder_store import TrimLadderStore
```

After the `trade_intent_store = TradeIntentStore(conn)` line (around line 61), add:

```python
    trim_ladder_store = TrimLadderStore(conn)
```

Then update the call to `build_phase2b_execution_chain` (around line 95) to pass the new store:

```python
    phase2b_chain = build_phase2b_execution_chain(
        policy, execution_store, gateway,
        trade_intent_store=trade_intent_store,
        trim_ladder_store=trim_ladder_store,
    )
```

After the existing `reconciler = ExecutionReconciler(...)` line (around line 142), add:

```python
    from agent.exit_ladder import ExitLadder
    exit_ladder = ExitLadder(policy, gateway, trim_ladder_store)
```

In the `try`/`finally` block at the bottom (around lines 149–154), update:

```python
    reader = SocketReader(socket_path)
    logger.info("Trading agent ready (shares-only). Listening on %s", socket_path)
    try:
        reconciler.start()
        exit_ladder.start()
        await reader.start(handle_event)
    finally:
        await exit_ladder.stop()
        await gateway.disconnect()
        await conn.close()
```

- [ ] **Step 12.6: Smoke-test main.py imports cleanly**

```bash
.venv/bin/python -c "import main"
```

Expected: no errors.

- [ ] **Step 12.7: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass. If `test_orchestrator.py` or any pre-existing chain-builder tests break, update them to pass `trim_ladder_store=MagicMock()`.

- [ ] **Step 12.8: Commit**

```bash
git add agent/registry.py main.py tests/unit/test_phase2b_chain_composition.py
git commit -m "wire: shares-only chain + ExitLadder background task in main"
```

---

## Task 13: Latency timestamps on the entry path

**Files:**
- Modify: `skills/execution/shares_market_submitter.py` (add timing log)
- Test: `tests/unit/test_shares_market_submitter.py` (assert timing log)

The spec lists a latency check as part of the work: `received_at` → `entry_ack_at` → `entry_filled_at`. The `received_at` already exists on context (set in main.py); `order_ack_at` and `filled_at` already get persisted by `update_execution_state`. The missing piece is a single log line at end-of-fill computing the deltas, so we can grep `logs/agent.log` for it.

- [ ] **Step 13.1: Add a latency-log assertion test**

In `tests/unit/test_shares_market_submitter.py`, add this test (you'll need to import `caplog` via the standard pytest `caplog` fixture):

```python
@pytest.mark.asyncio
async def test_logs_latency_from_received_at_to_filled(db, caplog):
    import logging
    caplog.set_level(logging.INFO)
    await _make_intent(db)
    gw, _, _ = _gateway(fill_qty=550, avg=145.32)
    intent_store = TradeIntentStore(db)
    trim_store = TrimLadderStore(db)
    skill = SharesMarketSubmitter(_policy(), gw, intent_store, trim_store)

    ctx = _ctx()
    ctx.update({"received_at": "2026-05-04T15:00:00+00:00"})
    await skill.run(ctx)

    latency_logs = [r for r in caplog.records
                    if "latency_ms" in r.getMessage()
                    and "shares_entry" in r.getMessage()]
    assert latency_logs, "expected one shares_entry latency log line"
```

- [ ] **Step 13.2: Run — expect FAIL**

```bash
.venv/bin/pytest tests/unit/test_shares_market_submitter.py::test_logs_latency_from_received_at_to_filled -v
```

Expected: FAIL.

- [ ] **Step 13.3: Add the latency log**

In `skills/execution/shares_market_submitter.py`, immediately before the final `return SkillResult(...)` block, add:

```python
        received_at_str = ctx.get("received_at")
        if received_at_str:
            try:
                received_at_dt = datetime.fromisoformat(received_at_str)
                fill_dt = datetime.fromisoformat(filled_at)
                latency_ms = int((fill_dt - received_at_dt).total_seconds() * 1000)
                logger.info("shares_entry latency_ms=%d ticker=%s qty=%d",
                            latency_ms, ctx.get("ticker"), fill.filled_qty)
            except (ValueError, TypeError):
                logger.warning("shares_entry: could not compute latency from received_at=%r",
                               received_at_str)
```

- [ ] **Step 13.4: Run — expect PASS**

```bash
.venv/bin/pytest tests/unit/test_shares_market_submitter.py -v
```

Expected: all pass.

- [ ] **Step 13.5: Commit**

```bash
git add skills/execution/shares_market_submitter.py tests/unit/test_shares_market_submitter.py
git commit -m "shares_submitter: log Discord→fill latency for SLO verification"
```

---

## Task 14: Final integration verification

This task is verification, not new code. Confirm the full system hangs together.

- [ ] **Step 14.1: Run the full test suite**

```bash
.venv/bin/pytest -q
```

Expected: zero failures. If any pre-existing tests break, fix them now (e.g., chain-builder tests that need `trim_ladder_store`).

- [ ] **Step 14.2: Boot main.py against an in-memory DB to verify wiring**

```bash
.venv/bin/python -c "
import asyncio, tempfile, os
from main import run
async def smoke():
    with tempfile.TemporaryDirectory() as td:
        sock = os.path.join(td, 'sock')
        db = os.path.join(td, 'agent.db')
        # Run for 2 seconds then cancel — proves imports + connect + start succeed
        task = asyncio.create_task(run(sock, db, 'config/policy.yaml'))
        await asyncio.sleep(2)
        task.cancel()
        try: await task
        except: pass
asyncio.run(smoke())
" 2>&1 | tail -20
```

Expected: log lines like "Trading agent ready (shares-only). Listening on ..." and "ExitLadder: started (interval=2s)". An IB connection failure is acceptable for this smoke test — we're just confirming the agent boots.

- [ ] **Step 14.3: Manual paper-account end-to-end check (deferred until ready to test live signals)**

Once the IB Gateway is up on paper:

1. Send a synthetic Discord signal (use `inject_event.py` if available, or write a one-off script that posts to the Unix socket).
2. Confirm the agent log shows: `TraderClassifier`, `TradeIntentWriter (instrument_type=equity)`, `RthEntryGuard`, `EquityContractBuilder`, `OrderSizer`, `SharesMarketSubmitter (filled)`, and a `shares_entry latency_ms=...` line.
3. Query the database:

```bash
sqlite3 data/trading_agent.db "SELECT intent_id, ticker, fill_price, fill_qty, execution_state FROM trade_intents ORDER BY created_at DESC LIMIT 1;"
sqlite3 data/trading_agent.db "SELECT intent_id, rung, threshold_pct, trim_pct, fired_at FROM trade_intent_trims ORDER BY armed_at DESC LIMIT 5;"
```

Expected: one filled equity intent, two armed trim rungs, both `fired_at` NULL.

4. Restart the agent process; tail the log to confirm the ExitLadder picks up the existing un-fired rungs and continues polling without re-entering.

- [ ] **Step 14.4: Final commit (only if cleanup needed)**

If steps 14.1–14.2 surfaced any small fixups:

```bash
git add <fixed files>
git commit -m "fixup: <what>"
```

---

## Summary

After Task 14 the system supports:

- Shares-only entry sized by conviction tier (HIGH=80% BP / LOW=40%)
- MKT order via `gateway.place_order` with new `order_type="MKT"` branch
- New `RthEntryGuard` blocks pre-market and after-hours entries
- New `EquityContractBuilder` qualifies the STK contract
- New `SharesMarketSubmitter` submits the MKT BUY, persists the fill, arms two trim rungs in `trade_intent_trims`
- New `ExitLadder` background task polls quotes every 2 s during RTH and fires MKT trim sells (40% at +5%, 40% at +10%, leaving 20% to hold forever)
- All trim-ladder state survives agent restarts

Live trading remains gated by `_assert_paper_guard()` — a follow-up spec is required to enable it.
