# Phase A+B: No-Trades Dedup, STW Sizing & Correctness Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore trading (fix the SameDayDedupGate self-suppression that fires zero trades), stop silently downsizing STW's high-conviction entries, and close a set of bounded correctness/reliability gaps — without changing trade-exit philosophy.

**Architecture:** Skill-chain pipeline (`agent/orchestrator.py` runs an ordered list of `Skill`s over a `Context`). Fixes are localized to individual skills, the SQLite stores, and the chain registry. Every fix is TDD: failing test → minimal change → green.

**Tech Stack:** Python 3.11+, `aiosqlite`, `pydantic` (policy), `pytest` + `pytest-asyncio` (asyncio_mode=auto), `ib_insync`.

**Out of scope (explicit user decisions):** No automated stop-loss / negative-stop rung, no automated options-exit ladder, no daily kill-switch. Downside = follow the trader's *explicit* sells, which is a **separate** feature (planned next). Phase C (execution quality) and Phase D (bigger subsystems) are separate plans.

**Branch:** `fix/no-trades-may-8` (current). One commit per task.

**Pre-flight:** Run `uv run pytest -q` and confirm the suite is green before starting (expected ~269 passed).

---

### Task 1: Fix SameDayDedupGate self-suppression (zero trades)

**Root cause:** `ClassificationLogger` (registry.py:30) writes the current event's `action_taken='fired'` row and commits *before* `SameDayDedupGate` (registry.py:32) runs. `has_fired_recently` then matches that just-written row (no `event_id` exclusion), forces `bucket=SKIP`, and `EntrySkipGate` halts. Every first-ever actionable signal self-suppresses.

**Fix:** Exclude the current `event_id` in `has_fired_recently`; pass `ctx.event_id` from the gate. Keeps the "log first, then dedup" order so the teaser→DD dedup (stocktalkweekly.yaml:45) still works: the first post excludes its own row (fires), the second post finds the first post's row (deduped).

**Files:**
- Modify: `infra/storage/classification_log_store.py:29-45`
- Modify: `skills/signal/same_day_dedup_gate.py:43-46`
- Modify: `tests/unit/test_same_day_dedup_gate.py:68` (mock signature)
- Test (create): `tests/integration/test_same_day_dedup_real_store.py`

- [ ] **Step 1: Write the failing real-store regression test**

Create `tests/integration/test_same_day_dedup_real_store.py`:

```python
import pytest
from agent.context import Context
from infra.storage.classification_log_store import ClassificationLogStore
from skills.signal.same_day_dedup_gate import SameDayDedupGate


async def _log_fired(store, *, event_id, trader="stocktalkweekly",
                     ticker="SEI", side="long"):
    await store.insert(
        event_id=event_id, trader_handle=trader, msg_text="OPENING $SEI",
        features={}, llm_response=None, bucket="HIGH", confidence=1.0,
        size_pct=0.0, size_source="shortcut_stated", action_taken="fired",
        reason="stated_size_in_message", ticker=ticker, side=side,
    )


@pytest.mark.asyncio
async def test_has_fired_recently_excludes_current_event(db):
    store = ClassificationLogStore(db)
    await _log_fired(store, event_id="e1")
    # The event that just logged its own 'fired' row must NOT see itself.
    assert await store.has_fired_recently(
        trader_handle="stocktalkweekly", ticker="SEI", side="long",
        hours=24, exclude_event_id="e1") is False
    # A different, later event DOES see e1 as a prior fire.
    assert await store.has_fired_recently(
        trader_handle="stocktalkweekly", ticker="SEI", side="long",
        hours=24, exclude_event_id="e2") is True


@pytest.mark.asyncio
async def test_first_signal_fires_second_is_deduped(db):
    """Regression for fix/no-trades-may-8: the FIRST (trader,ticker,side)
    must fire; only an identical SECOND within the window is suppressed."""
    store = ClassificationLogStore(db)
    gate = SameDayDedupGate(store, window_hours=24)

    await _log_fired(store, event_id="e1")            # logger already ran
    ctx1 = Context(trace_id="t1", event_id="e1")
    ctx1.update({"bucket": "HIGH", "ticker": "SEI", "side": "long",
                 "trader_handle": "stocktalkweekly"})
    r1 = await gate.run(ctx1)
    assert r1.status == "success", "first-ever signal must NOT self-suppress"
    assert ctx1.get("bucket") == "HIGH"

    await _log_fired(store, event_id="e2")            # identical repost
    ctx2 = Context(trace_id="t2", event_id="e2")
    ctx2.update({"bucket": "HIGH", "ticker": "SEI", "side": "long",
                 "trader_handle": "stocktalkweekly"})
    r2 = await gate.run(ctx2)
    assert r2.status == "skip"
    assert ctx2.get("bucket") == "SKIP"
    assert ctx2.get("size_source") == "dedup"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/test_same_day_dedup_real_store.py -v`
Expected: FAIL — `TypeError: has_fired_recently() got an unexpected keyword argument 'exclude_event_id'`

- [ ] **Step 3: Add the `exclude_event_id` parameter to the store**

In `infra/storage/classification_log_store.py`, replace `has_fired_recently` (lines 29-45):

```python
    async def has_fired_recently(self, *, trader_handle: str, ticker: str,
                                 side: str, hours: float,
                                 exclude_event_id: str | None = None) -> bool:
        """True if a 'fired' classification for (trader, ticker, side) exists
        within the last `hours`, EXCLUDING `exclude_event_id`.

        The exclusion is essential: SameDayDedupGate runs AFTER
        ClassificationLogger has already committed the current event's own
        'fired' row, so without excluding it the gate would match that row and
        suppress the very first signal (the fix/no-trades-may-8 regression)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        sql = ["""SELECT 1 FROM classification_log
                  WHERE trader_handle = ?
                    AND ticker = ?
                    AND side = ?
                    AND action_taken = 'fired'
                    AND created_at >= ?"""]
        params = [trader_handle, ticker, side, cutoff]
        if exclude_event_id is not None:
            sql.append("AND event_id != ?")
            params.append(exclude_event_id)
        sql.append("LIMIT 1")
        cursor = await self._conn.execute("\n".join(sql), params)
        return (await cursor.fetchone()) is not None
```

- [ ] **Step 4: Pass `ctx.event_id` from the gate**

In `skills/signal/same_day_dedup_gate.py`, replace the `has_fired_recently` call (lines 43-46):

```python
        fired = await self._store.has_fired_recently(
            trader_handle=trader, ticker=ticker, side=side,
            hours=self._window_hours, exclude_event_id=ctx.event_id,
        )
```

- [ ] **Step 5: Update the existing unit test's mock signature**

In `tests/unit/test_same_day_dedup_gate.py`, change `maybe_fired` (line 68) so it accepts the new kwarg:

```python
    async def maybe_fired(*, trader_handle, ticker, side, hours,
                          exclude_event_id=None):
        return side == "long"  # only long has fired
```

- [ ] **Step 6: Run all dedup tests**

Run: `uv run pytest tests/integration/test_same_day_dedup_real_store.py tests/unit/test_same_day_dedup_gate.py -v`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add infra/storage/classification_log_store.py skills/signal/same_day_dedup_gate.py tests/integration/test_same_day_dedup_real_store.py tests/unit/test_same_day_dedup_gate.py
git commit -m "fix(signal): dedup gate no longer self-suppresses the first signal

has_fired_recently now excludes the current event_id, so a freshly-logged
'fired' row can't suppress its own signal. Restores trading (was firing zero
trades). Adds a real-SQLite regression test (first fires, identical second
within window deduped)."
```

---

### Task 2: Stop downsizing STW's high-conviction entries (size_floor)

**Root cause:** `trader_classifier.py:71` (shortcut) and `:169-174` (WSE override) force `LOW` when stated size < 7.5%. STW posts 1–2% but its profile documents "every STW entry is sized HIGH … the 1% is informational only" (stocktalkweekly.yaml:14-15). Result: STW's best signals deploy the LOW tier (0.08 vs 0.10 shares).

**Fix:** Add an optional `size_floor` to `TraderProfile`. When `size_floor == "HIGH"`, an actionable entry is lifted LOW→HIGH in both the shortcut and the LLM path. It NEVER overrides a `SKIP` (the <0.50 confidence and ticker-not-in-message anti-hallucination guards stay intact).

**Files:**
- Modify: `agent/traders/profile.py:17-30,46-59`
- Modify: `skills/signal/trader_classifier.py:64-83,169-185`
- Modify: `config/traders/stocktalkweekly.yaml:9`
- Modify: `tests/unit/test_trader_classifier.py:9` (helper) + new tests
- Modify: `tests/unit/test_seed_profiles.py` (new test)

- [ ] **Step 1: Write failing classifier tests**

In `tests/unit/test_trader_classifier.py`, change the `make_profile` signature (line 9) and its return to thread `size_floor`:

```python
def make_profile(handle="wse", auto=True, size_in_msg=True, size_floor=None) -> TraderProfile:
    return TraderProfile(
        handle=handle, display_name="Wall St Engine",
        discord_author_pattern="Wall St Engine",
        alert_mention="@Wall - Alerts", require_alert_mention=True,
        bot_authors_to_skip=(), auto_execute=auto,
        size_in_message=size_in_msg, prefer_message_size=True,
        classifier_model="claude-haiku-4-5",
        availability_phrases=(),
        conviction_examples=(
            ConvictionExample(msg="Added 2% pos AUDC", bucket="LOW", why="2% small"),
            ConvictionExample(msg="upsizing core ENS aggressively", bucket="HIGH", why="upsize core"),
            ConvictionExample(msg="watching TEST closely", bucket="SKIP", why="no entry"),
        ),
        size_floor=size_floor,
    )
```

Append these tests to the file:

```python
@pytest.mark.asyncio
async def test_stw_size_floor_shortcut_forces_high():
    profile = make_profile(handle="stocktalkweekly", size_floor="HIGH")
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "SEI", "side": "long",
                   "bucket": "LOW", "confidence": 0.5, "reason": "unused"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly", "trader_handle": "stocktalkweekly",
        "full_message_text": "OPENING $SEI with a small 1% pos",
    })
    result = await classifier.run(ctx)
    assert result.status == "success"
    assert ctx.get("bucket") == "HIGH"          # 1% would be LOW without the floor
    assert ctx.get("size_source") == "shortcut_stated"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_stw_size_floor_llm_path_forces_high():
    profile = make_profile(handle="stocktalkweekly", size_in_msg=False, size_floor="HIGH")
    registry = TraderRegistry([profile])
    # 0.65 confidence would normally downgrade HIGH->LOW; no entry verb so the
    # shortcut does not fire and we exercise the LLM path.
    llm = FakeLLM({"is_entry": True, "ticker": "ADEA", "side": "long",
                   "bucket": "HIGH", "confidence": 0.65, "reason": "thesis"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly", "trader_handle": "stocktalkweekly",
        "full_message_text": "ADEIA $ADEA multi-pillar thesis, a 2% position",
    })
    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_source") == "size_floor"


@pytest.mark.asyncio
async def test_stw_size_floor_does_not_rescue_low_confidence_skip():
    profile = make_profile(handle="stocktalkweekly", size_in_msg=False, size_floor="HIGH")
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "SEI", "side": "long",
                   "bucket": "LOW", "confidence": 0.3, "reason": "ambiguous"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Stock Talk Weekly", "trader_handle": "stocktalkweekly",
        "full_message_text": "maybe SEI here",
    })
    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "SKIP"          # floor must not override the SKIP guard
    assert ctx.get("size_source") == "drop_low_conf"


@pytest.mark.asyncio
async def test_wse_small_size_override_unaffected_without_floor():
    profile = make_profile(handle="wse", size_in_msg=False, size_floor=None)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "AUDC", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "thesis"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "AUDC 2% weighting compelling setup",  # no entry verb
    })
    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "LOW"
    assert ctx.get("size_source") == "wse_small_size_override"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_trader_classifier.py -k "size_floor or override_unaffected" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'size_floor'` (TraderProfile has no such field yet)

- [ ] **Step 3: Add `size_floor` to the profile model**

In `agent/traders/profile.py`, add the field at the end of `TraderProfile` (after `conviction_examples`, line 30):

```python
    conviction_examples: tuple[ConvictionExample, ...]
    size_floor: str | None = None  # "HIGH" -> every actionable entry sized HIGH
```

In `load_profile`, validate and load it. After the examples loop (line 45), before the `return`:

```python
    size_floor = raw.get("size_floor")
    if size_floor is not None and size_floor not in VALID_BUCKETS:
        raise ValueError(f"invalid size_floor {size_floor!r} in {path}")
```

And add to the `TraderProfile(...)` return (after `conviction_examples=tuple(examples),`):

```python
        conviction_examples=tuple(examples),
        size_floor=size_floor,
    )
```

- [ ] **Step 4: Apply the floor in the classifier shortcut**

In `skills/signal/trader_classifier.py`, inside the shortcut block, replace the bucket assignment (line 71):

```python
            bucket = "HIGH" if features.stated_size_pct >= SMALL_SIZE_THRESHOLD else "LOW"
            if profile.size_floor == "HIGH":
                bucket = "HIGH"
```

- [ ] **Step 5: Apply the floor in the LLM path**

In the same file, after the WSE small-size override block (after line 174, before the final `updates = {` at line 176), add:

```python
        # Per-trader size floor: a trader can be configured so every actionable
        # entry is HIGH conviction regardless of stated size or the confidence
        # downgrade (e.g. STW, where the stated % is documented as informational).
        # Only lifts LOW->HIGH; the SKIP guards above already returned.
        if profile.size_floor == "HIGH" and final_bucket == "LOW":
            final_bucket = "HIGH"
            size_source = "size_floor"
```

- [ ] **Step 6: Set the floor on the STW profile**

In `config/traders/stocktalkweekly.yaml`, add after line 9 (`prefer_message_size: true`):

```yaml
size_floor: HIGH
```

- [ ] **Step 7: Add the profile-load test**

Append to `tests/unit/test_seed_profiles.py`:

```python
def test_stocktalkweekly_has_high_size_floor():
    profiles = load_all_profiles(TRADERS_DIR)
    stw = next(p for p in profiles if p.handle == "stocktalkweekly")
    assert stw.size_floor == "HIGH"
```

- [ ] **Step 8: Run the affected tests**

Run: `uv run pytest tests/unit/test_trader_classifier.py tests/unit/test_seed_profiles.py -v`
Expected: PASS (all, including the pre-existing classifier tests — none pass `size_floor`, so they default to None and are unaffected)

- [ ] **Step 9: Commit**

```bash
git add agent/traders/profile.py skills/signal/trader_classifier.py config/traders/stocktalkweekly.yaml tests/unit/test_trader_classifier.py tests/unit/test_seed_profiles.py
git commit -m "feat(signal): per-trader size_floor; STW entries always sized HIGH

Stated small % on STW (1-2%) was forcing LOW via the shortcut and WSE
override, half-sizing the highest-trust source. size_floor: HIGH lifts
actionable entries LOW->HIGH in both paths without overriding SKIP guards."
```

---

### Task 3: Persist filled_at + broker_order_ref on fill (revives cooldown)

**Root cause:** The shares fill path calls `update_fill` (trade_intent_store.py:91-105), which sets `execution_state='filled'` but never sets `filled_at` or `broker_order_ref`. `CooldownGuard.get_filled_since` filters `filled_at >= since`, so it never matches a real fill → cooldown is a no-op. Order traceability is also lost.

**Fix:** `update_fill` sets `filled_at` (now) and an optional `broker_order_ref`; submitters pass `fill.broker_order_id`. The options leg's `write()` also records `filled_at` + `broker_order_ref`.

**Files:**
- Modify: `infra/storage/trade_intent_store.py:91-105,138-179`
- Modify: `skills/execution/shares_market_submitter.py:46-49`
- Modify: `skills/execution/options_market_submitter.py:56-74`
- Modify: `tests/integration/test_trade_intent_store.py` (new tests)

- [ ] **Step 1: Write failing store tests**

Append to `tests/integration/test_trade_intent_store.py`:

```python
async def test_update_fill_sets_filled_at_and_broker_ref(db):
    store = TradeIntentStore(db)
    await store.insert(_base_intent("evt9:NVDA:long"))
    await store.update_fill(
        "evt9:NVDA:long", fill_price=12.5, fill_qty=10,
        broker_order_ref="IB-123",
    )
    row = await store.get("evt9:NVDA:long")
    assert row["execution_state"] == "filled"
    assert row["fill_price"] == pytest.approx(12.5)
    assert row["fill_qty"] == 10
    assert row["broker_order_ref"] == "IB-123"
    assert row["filled_at"] is not None
    # Cooldown revival: get_filled_since now finds the fill.
    rows = await store.get_filled_since("NVDA", "2020-01-01T00:00:00+00:00")
    assert len(rows) == 1


async def test_write_records_filled_at_and_broker_ref(db):
    store = TradeIntentStore(db)
    now = _now()
    await store.write(
        intent_id="evt9:NVDA:option", event_id="evt9", channel="mystic",
        ticker="NVDA", side="long", instrument_type="option",
        parent_intent_id="evt9:NVDA:long", expiry="2027-01-15", strike=150.0,
        right="C", conviction="HIGH", fill_price=3.2, fill_qty=2,
        execution_state="filled", signal_received_at=now,
        broker_order_ref="IB-456",
    )
    row = await store.get("evt9:NVDA:option")
    assert row["broker_order_ref"] == "IB-456"
    assert row["filled_at"] is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_trade_intent_store.py -k "filled_at" -v`
Expected: FAIL — `update_fill()` rejects `broker_order_ref`; `write()` rejects `broker_order_ref`.

- [ ] **Step 3: Enhance `update_fill`**

In `infra/storage/trade_intent_store.py`, replace `update_fill` (lines 91-105):

```python
    async def update_fill(
        self,
        intent_id: str,
        *,
        fill_price: float,
        fill_qty: int,
        execution_state: str = "filled",
        broker_order_ref: str | None = None,
        filled_at: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE trade_intents SET fill_price=?, fill_qty=?, execution_state=?, "
            "filled_at=?, broker_order_ref=?, updated_at=? WHERE intent_id=?",
            (fill_price, fill_qty, execution_state, filled_at or now,
             broker_order_ref, now, intent_id),
        )
        await self._conn.commit()
```

- [ ] **Step 4: Record filled_at + broker_order_ref in `write()`**

In the same file, add a `broker_order_ref` parameter to `write()` (after `signal_received_at: str,` at line 155) and set `filled_at` + `broker_order_ref` in the record. Change the signature line and the record dict:

```python
        signal_received_at: str,
        broker_order_ref: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "intent_id": intent_id,
            "event_id": event_id,
            "channel": channel,
            "ticker": ticker,
            "side": side,
            "instrument_type": instrument_type,
            "parent_intent_id": parent_intent_id,
            "expiry": expiry,
            "strike": strike,
            "right": right,
            "conviction": conviction,
            "fill_price": fill_price,
            "fill_qty": fill_qty,
            "execution_state": execution_state,
            "broker_order_ref": broker_order_ref,
            "filled_at": now if execution_state == "filled" else None,
            "policy_state": "approved",
            "signal_received_at": signal_received_at,
            "intent_created_at": now,
            "created_at": now,
            "updated_at": now,
        }
        await self.insert(record)
```

- [ ] **Step 5: Pass broker_order_ref from the shares submitter**

In `skills/execution/shares_market_submitter.py`, replace the `update_fill` call (lines 46-49):

```python
        await self._intents.update_fill(
            intent_id, fill_price=fill.avg_fill_price or 0.0,
            fill_qty=fill.filled_qty, broker_order_ref=fill.broker_order_id,
        )
```

- [ ] **Step 6: Pass broker_order_ref from the options submitter**

In `skills/execution/options_market_submitter.py`, add to the `self._intents.write(...)` call (it ends at line 74 with `signal_received_at=...`); add the argument:

```python
            signal_received_at=ctx.get("received_at",
                                       datetime.now(timezone.utc).isoformat()),
            broker_order_ref=fill.broker_order_id,
        )
```

- [ ] **Step 7: Run store + submitter tests**

Run: `uv run pytest tests/integration/test_trade_intent_store.py tests/unit/test_shares_market_submitter.py tests/unit/test_options_market_submitter.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add infra/storage/trade_intent_store.py skills/execution/shares_market_submitter.py skills/execution/options_market_submitter.py tests/integration/test_trade_intent_store.py
git commit -m "fix(execution): persist filled_at + broker_order_ref on fill

update_fill never set filled_at, so CooldownGuard.get_filled_since (which
filters on filled_at) never matched a real fill -- cooldown was a no-op.
Now both legs record filled_at and the IB order id for traceability."
```

---

### Task 4: Set PRAGMA busy_timeout on every connection

**Root cause:** `get_connection` (db.py:209-218) sets WAL/synchronous/foreign_keys but not `busy_timeout`. Any second connection (e.g. `bin/audit_trader_patterns.py` run during a live trade) makes one side get an instant "database is locked".

**Files:**
- Modify: `infra/storage/db.py:209-218`
- Test (create or append): `tests/unit/test_db_schema.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_db_schema.py`:

```python
@pytest.mark.asyncio
async def test_busy_timeout_is_set(tmp_path):
    from infra.storage.db import get_connection
    conn = await get_connection(str(tmp_path / "t.db"))
    try:
        async with conn.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
        assert row[0] == 5000
    finally:
        await conn.close()
```

(If `test_db_schema.py` lacks `import pytest`, add it at the top.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_db_schema.py::test_busy_timeout_is_set -v`
Expected: FAIL — `assert 0 == 5000`

- [ ] **Step 3: Add the pragma**

In `infra/storage/db.py`, in `get_connection`, add after line 214 (`PRAGMA foreign_keys=ON`):

```python
    await conn.execute("PRAGMA busy_timeout=5000")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_db_schema.py::test_busy_timeout_is_set -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add infra/storage/db.py tests/unit/test_db_schema.py
git commit -m "fix(storage): set PRAGMA busy_timeout=5000 on every connection

Prevents instant 'database is locked' when a second connection (audit/promote
scripts) touches agent.db during a live trade."
```

---

### Task 5: Persist the real message_fingerprint

**Root cause:** `main.py:162` hard-codes `"message_fingerprint": ""` in the `signal_events` insert (the insert happens before the pipeline computes the fingerprint). The normalizer computes a real one but it is never stored on the raw-signal row.

**Fix:** Extract the fingerprint logic into a pure helper, reuse it in both `MessageNormalizer` and `main.py`.

**Files:**
- Modify: `skills/signal/message_normalizer.py:14-33`
- Modify: `main.py` (import + handle_event insert)
- Test (append): `tests/unit/test_message_normalizer.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_message_normalizer.py`:

```python
def test_compute_fingerprint_is_deterministic_and_whitespace_normalized():
    from skills.signal.message_normalizer import compute_fingerprint
    fp1 = compute_fingerprint("mystic", "Mystic", "long  $SPY   now")
    fp2 = compute_fingerprint("mystic", "Mystic", "long $SPY now")
    assert fp1 == fp2
    assert len(fp1) == 16
    assert compute_fingerprint("wse", "WSE", "x") != fp1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_message_normalizer.py::test_compute_fingerprint_is_deterministic_and_whitespace_normalized -v`
Expected: FAIL — `ImportError: cannot import name 'compute_fingerprint'`

- [ ] **Step 3: Extract the helper**

In `skills/signal/message_normalizer.py`, add a module-level function (after the imports, before the class):

```python
def compute_fingerprint(channel: str, author: str, text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(
        f"{channel}:{author}:{normalized}".encode()
    ).hexdigest()[:16]
```

Replace the fingerprint line inside `run` (lines 20-22) to reuse it:

```python
        normalized = re.sub(r"\s+", " ", preview).strip()
        fingerprint = compute_fingerprint(channel, author, preview)
```

- [ ] **Step 4: Use it in main.py**

In `main.py`, add to the imports near the other skill imports:

```python
from skills.signal.message_normalizer import compute_fingerprint
```

In `handle_event`, replace the `signal_store.insert({...})` `message_fingerprint` line (main.py:162):

```python
            "message_fingerprint": compute_fingerprint(
                event.channel, event.author, event.trigger_preview),
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/unit/test_message_normalizer.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add skills/signal/message_normalizer.py main.py tests/unit/test_message_normalizer.py
git commit -m "fix(signal): persist real message_fingerprint on signal_events

Extract compute_fingerprint helper and use it for the raw-signal insert so
the stored fingerprint matches the idempotency key instead of an empty string."
```

---

### Task 6: Remove retired DesktopReader from the live chain

**Root cause:** `DesktopReader` (registry.py:27) runs a full-screen `screencapture` + vision-LLM fallback for messages under 40 chars / containing `#`. The Chrome extension is the sole capture path and already forwards full message text (memory: bridge retired 2026-05-08), so this only adds up to ~18s latency and a screenshot-of-your-screen risk. `MessageNormalizer` (which runs before it) already sets `full_message_text`/`capture_mode`.

**Fix:** Unwire `DesktopReader` from `build_phase1_chain` (keep the file in case it is ever revived). Add a composition test (also closes the audit's "phase1 chain untested" gap).

**Files:**
- Modify: `agent/registry.py:8,27`
- Test (create): `tests/unit/test_registry_phase1_chain.py`

- [ ] **Step 1: Write the failing composition test**

Create `tests/unit/test_registry_phase1_chain.py`:

```python
from unittest.mock import MagicMock
from agent.registry import build_phase1_chain


def _chain():
    return build_phase1_chain(
        MagicMock(), idempotency_store=MagicMock(), telegram_client=MagicMock(),
        gateway=MagicMock(), trader_registry=MagicMock(),
        classification_log_store=MagicMock(), llm_classifier=MagicMock(),
    )


def test_phase1_chain_excludes_desktop_reader():
    names = [s.name for s in _chain()]
    assert "desktop_reader" not in names


def test_phase1_chain_keeps_core_skills_in_order():
    names = [s.name for s in _chain()]
    # ClassificationLogger must precede SameDayDedupGate (dedup reads its log row)
    assert names.index("ClassificationLogger") < names.index("SameDayDedupGate")
    for expected in ("message_normalizer", "TraderClassifier",
                     "SameDayDedupGate", "EntrySkipGate"):
        assert expected in names
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_registry_phase1_chain.py -v`
Expected: FAIL — `assert "desktop_reader" not in names` (it is still present)

- [ ] **Step 3: Unwire DesktopReader**

In `agent/registry.py`, delete the import (line 8):

```python
    from skills.signal.desktop_reader import DesktopReader
```

and delete the list entry (line 27):

```python
        DesktopReader(policy),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_registry_phase1_chain.py tests/unit/test_desktop_reader.py -v`
Expected: PASS (the DesktopReader unit tests still pass — the class is unchanged, just unwired)

- [ ] **Step 5: Commit**

```bash
git add agent/registry.py tests/unit/test_registry_phase1_chain.py
git commit -m "refactor(signal): unwire retired DesktopReader from live chain

The Chrome extension forwards full message text, so the screenshot+vision
fallback only added latency and a screenshot risk. Keeps the file; adds a
phase1-chain composition test (incl. logger-before-dedup ordering)."
```

---

### Task 7: Delete dead parallel classifiers

**Root cause:** `skills/signal/ticker_resolver.py` and `skills/signal/trade_intent_detector.py` have zero production references (the live classifier is `TraderClassifier`). Each instantiates `anthropic.AsyncAnthropic()` and carries its own prompt — a "fix the wrong classifier" trap and latent double-LLM-bill risk.

**Files:**
- Delete: `skills/signal/ticker_resolver.py`, `skills/signal/trade_intent_detector.py`, and any of their test files.

- [ ] **Step 1: Confirm there are no production references**

Run:
```bash
grep -rn "ticker_resolver\|trade_intent_detector\|TickerResolver\|TradeIntentDetector" agent/ skills/ infra/ main.py inject_event.py bin/
git ls-files | grep -E "ticker_resolver|trade_intent_detector"
```
Expected: matches ONLY in the two source files and their own test files (no imports from `agent/`, `skills/` non-test, `infra/`, `main.py`). If anything else references them, STOP and report.

- [ ] **Step 2: Delete the files (and their tests)**

```bash
git rm skills/signal/ticker_resolver.py skills/signal/trade_intent_detector.py
# delete test files only if step 1 found them:
git rm tests/unit/test_ticker_resolver.py 2>/dev/null || true
git rm tests/unit/test_trade_intent_detector.py 2>/dev/null || true
```

- [ ] **Step 3: Run the full suite to confirm nothing breaks**

Run: `uv run pytest -q`
Expected: PASS (collection succeeds with no import errors)

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(signal): delete dead parallel classifiers

ticker_resolver and trade_intent_detector had no production references (live
classifier is TraderClassifier). Removes a wrong-classifier-edit trap and a
latent double-LLM-billing path."
```

---

### Task 8: get_chain accepts a single valid candidate

**Root cause:** `gateway.py:255-257` hard-fails (`raise IBGatewayUnavailable`) and records a read-breaker failure whenever fewer than 2 option candidates survive qualify+quote. `ContractSelector` only needs one. This drops valid single-contract setups and can trip the read breaker on thin names.

**Files:**
- Modify: `infra/ib/gateway.py:255-257`
- Test (append): `tests/unit/test_gateway_get_chain.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_gateway_get_chain.py`:

```python
async def test_get_chain_returns_single_candidate():
    from datetime import date, timedelta
    far_expiry = (date.today() + timedelta(days=200)).strftime("%Y%m%d")

    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()
    stock_ref = MagicMock(); stock_ref.conId = 12345

    chain = _make_chain(strikes=[150.0, 152.0, 155.0, 160.0], expirations=[far_expiry])
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    opt_calls = [0]
    async def one_survivor(contract):
        if not hasattr(contract, 'secType') or contract.secType != "OPT":
            return [stock_ref]
        opt_calls[0] += 1
        if opt_calls[0] >= 2:
            return []  # only the first option qualifies
        c = MagicMock()
        c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike; c.right = contract.right
        c.multiplier = "100"; c.localSymbol = None; c.tradingClass = None
        return [c]

    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=one_survivor)
    td = MagicMock(); td.bid = 2.0; td.ask = 2.5
    gw._ib.reqTickersAsync = AsyncMock(return_value=[td])

    candidates = await gw.get_chain("NVDA", spot_price=152.0)
    assert len(candidates) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_gateway_get_chain.py::test_get_chain_returns_single_candidate -v`
Expected: FAIL — raises `IBGatewayUnavailable("chain_lookup_insufficient_candidates")`

- [ ] **Step 3: Lower the threshold**

In `infra/ib/gateway.py`, replace lines 255-257:

```python
            if len(candidates) < 1:
                self._read_breaker._record_failure()
                raise IBGatewayUnavailable("chain_lookup_no_candidates")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_gateway_get_chain.py -v`
Expected: PASS (existing chain tests still pass — they produce ≥1 candidate)

- [ ] **Step 5: Commit**

```bash
git add infra/ib/gateway.py tests/unit/test_gateway_get_chain.py
git commit -m "fix(ib): accept a single valid option candidate in get_chain

Was hard-failing + tripping the read breaker when <2 candidates survived,
dropping valid single-contract setups. ContractSelector needs only one."
```

---

## Final verification

- [ ] Run the whole suite: `uv run pytest -q` — expected: all green (≈269 + new tests).
- [ ] Sanity-check the headline fix end-to-end if a paper IB session is available: inject one actionable signal and confirm it is no longer skipped with `size_source=dedup`.

## Self-review (completed during authoring)

- **Spec coverage:** All 8 Phase A+B items have a task (dedup, STW size_floor, fill persistence/cooldown, busy_timeout, message_fingerprint, DesktopReader unwire, dead-code delete, get_chain ≥1). Dropped items (auto stop-loss, auto options-exit) intentionally absent. Sell-following deferred to a separate plan.
- **Placeholder scan:** No TBD/placeholder steps; every code/test step shows full code.
- **Type consistency:** `size_floor` defined on `TraderProfile` (Task 2) and read as `profile.size_floor` in the classifier; `has_fired_recently(..., exclude_event_id=...)` defined (Task 1) and called with `ctx.event_id`; `update_fill(..., broker_order_ref=, filled_at=)` and `write(..., broker_order_ref=)` defined (Task 3) and called with `fill.broker_order_id`; `compute_fingerprint(channel, author, text)` defined (Task 5) and called consistently.
