# In-Flight Sell Reserve — §3a Oversell Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `remaining_qty` account for an in-flight trader-sell (symmetric to the existing in-flight trim reserve) so a concurrent trim or second-fingerprint sell cannot oversize, closing the spec §3a oversell.

**Architecture:** Reuse the existing `position_exits.requested_qty`/`sold_qty` columns: a row with `sold_qty IS NULL` is an in-flight reservation of `requested_qty`; finalizing sets `sold_qty` to the actual fill. `follow_sell_position` reserves the full qty *before* `place_order` and finalizes *after* the fill, so a concurrent `fire_rung_if_crossed` reads `remaining_qty = 0` during the sell's window and short-circuits. No schema migration (the columns already exist).

**Tech Stack:** Python 3.11+, asyncio, aiosqlite (SQLite), pytest + pytest-asyncio (`asyncio_mode = "auto"`), Hypothesis stateful testing.

## Global Constraints

- **Repo:** `/Users/jasonli/dev/trading-agent`, branch `fix/inflight-sell-reserve` (already created off `feat/money-safety-invariant-suite`; PR #14 still open).
- **Test runner:** `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest` — the committed `venv/` dir lacks `hypothesis`; `uv` manages `.venv` and installs the `dev` extra. Do NOT use bare `pytest` or `venv/bin/python`.
- **Baseline:** the full suite is `516 passed, 1 xfailed` (the §3a probe). Every task must leave the suite green; the only intended count change is the `1 xfailed` becoming a normal pass plus the new tests.
- **No schema migration.** `position_exits.requested_qty` (INTEGER) and `sold_qty` (INTEGER, nullable) already exist (`infra/storage/db.py`).
- **Keep `record_exit` unchanged** — still valid for direct-record callers/tests. After this work it has no production caller (only tests), which is fine.
- **Keep `sold_qty_for_intent` unchanged** — it is `SUM(sold_qty)` = the ACTUAL-sold total used by P&L/exposure semantics; it must NEVER count reserves.
- **`skills/risk/exposure.py` gets NO production change** — its `SUM(e.sold_qty)` already ignores NULL pending rows, which is correct (the shares are still held until the sell finalizes). Only a guard test is added.
- **Reserve disposition matrix (spec §3.3), authoritative:**
  | Event | Disposition |
  |---|---|
  | `place_order` raises (nothing placed) | `finalize_exit(sold_qty=0)` → release; re-raise |
  | `wait_fill` raises (order may be live) | keep reserve → stuck-until-reconciled (safe) |
  | zero-fill | `finalize_exit(sold_qty=0)` → release |
  | partial fill (N<req) | `finalize_exit(sold_qty=N)` → release over-reserve |
  | full fill | `finalize_exit(sold_qty=req)` |
  | crash between reserve and finalize | stuck reserve (safe; never oversells); accepted |
- **Conventional Commits.** Co-locate tests. Match each test file's local style (some use `@pytest.mark.asyncio`, some rely on `asyncio_mode=auto` — follow the file you edit).

---

## File Structure

All paths relative to `/Users/jasonli/dev/trading-agent`. No new files.

- `infra/storage/position_exit_store.py` — **Modify.** Add `reserve_exit` + `finalize_exit`; change `remaining_qty`'s exit term to count pending `requested_qty`. (Task 1)
- `tests/integration/test_position_exit_store.py` — **Modify.** Unit tests for reserve/finalize. (Task 1)
- `skills/execution/sell_follower.py` — **Modify.** `follow_sell_position`: reserve before place, finalize after fill. (Task 2)
- `tests/integration/test_sell_follower.py` — **Modify.** Add reserve-before-place timing test; import `follow_sell_position`. (Task 2)
- `tests/property/test_inflight_sell_oversell.py` — **Modify.** Remove the `xfail(strict=True)` marker (the §3a probe now passes). (Task 2)
- `bin/pnl_report.py` — **Modify.** Add `WHERE sold_qty IS NOT NULL` to the exits query. (Task 3)
- `tests/integration/test_pnl_report_cli.py` — **Modify.** Test `_fetch` drops pending reserves, keeps finalized exits. (Task 3)
- `tests/unit/test_exposure.py` — **Modify.** Guard test: a pending reserve does not reduce exposure. (Task 3)
- `tests/property/test_position_invariants.py` — **Modify.** Add the N3 in-flight-sell rule; keep the `remaining_qty_identity` oracle faithful to the new exit term. (Task 4)

**Why §3a's marker removal lives in Task 2:** `test_inflight_sell_oversell.py` is marked `xfail(strict=True)`. The follower change (Task 2) makes its assertion pass, which under `strict=True` turns an XPASS into a FAILURE. So the marker MUST be removed in the same commit that lands the follower fix, or that commit is red.

---

### Task 1: Store — `reserve_exit`, `finalize_exit`, and reserve-aware `remaining_qty`

**Files:**
- Modify: `infra/storage/position_exit_store.py`
- Test: `tests/integration/test_position_exit_store.py`

**Interfaces:**
- Produces:
  - `async def reserve_exit(self, *, fingerprint: str, event_id: str | None, intent_id: str, channel: str | None, ticker: str | None, scope: str, requested_qty: int, reason: str | None) -> int` — INSERTs a pending row (`sold_qty=NULL`), returns the new row id.
  - `async def finalize_exit(self, exit_id: int, *, sold_qty: int, sold_avg_price: float | None, broker_order_ref: str | None, reason: str | None) -> None` — UPDATEs the row's `sold_qty`/`sold_avg_price`/`broker_order_ref`/`reason` by id.
  - `remaining_qty(intent_id)` now nets `SUM(CASE WHEN sold_qty IS NOT NULL THEN sold_qty ELSE COALESCE(requested_qty,0) END)` for exits.
- Consumes: existing `_intent` helper and `TradeIntentStore`/`PositionExitStore` already imported in the test file; existing `datetime`/`timezone` already imported in the store.

- [ ] **Step 1: Write the failing unit tests**

In `tests/integration/test_position_exit_store.py`, append these three tests at the end of the file (after `test_migration_creates_exit_tables_on_legacy_db`). They use the existing module-level `_intent(...)` helper:

```python
async def test_reserve_exit_reserves_full_requested_qty(db):
    intents = TradeIntentStore(db)
    exits = PositionExitStore(db)
    await intents.insert(_intent("e1:AAPL:long", fill_qty=100))
    assert await exits.remaining_qty("e1:AAPL:long") == 100

    exit_id = await exits.reserve_exit(
        fingerprint="fp", event_id="e", intent_id="e1:AAPL:long",
        channel="mystic", ticker="AAPL", scope="full", requested_qty=60,
        reason="follow_sell_pending")
    assert isinstance(exit_id, int) and exit_id > 0
    # remaining_qty reserves the full requested qty while the sell is in-flight.
    assert await exits.remaining_qty("e1:AAPL:long") == 40
    # A pending reserve is NOT an actual sale: P&L/exposure totals must ignore it.
    assert await exits.sold_qty_for_intent("e1:AAPL:long") == 0


async def test_finalize_exit_corrects_to_actual_and_releases_over_reserve(db):
    intents = TradeIntentStore(db)
    exits = PositionExitStore(db)
    await intents.insert(_intent("e1:AAPL:long", fill_qty=100))
    exit_id = await exits.reserve_exit(
        fingerprint="fp", event_id="e", intent_id="e1:AAPL:long",
        channel="mystic", ticker="AAPL", scope="full", requested_qty=60,
        reason="follow_sell_pending")
    assert await exits.remaining_qty("e1:AAPL:long") == 40

    # Only 40 actually filled -> the 20-share over-reserve is released.
    await exits.finalize_exit(exit_id, sold_qty=40, sold_avg_price=99.0,
                              broker_order_ref="IB-1", reason="follow_sell")
    assert await exits.remaining_qty("e1:AAPL:long") == 60
    assert await exits.sold_qty_for_intent("e1:AAPL:long") == 40


async def test_finalize_exit_zero_releases_full_reserve(db):
    # place_order-failure / zero-fill path: finalize sold_qty=0 frees the reserve.
    intents = TradeIntentStore(db)
    exits = PositionExitStore(db)
    await intents.insert(_intent("e1:AAPL:long", fill_qty=100))
    exit_id = await exits.reserve_exit(
        fingerprint="fp", event_id="e", intent_id="e1:AAPL:long",
        channel="mystic", ticker="AAPL", scope="full", requested_qty=100,
        reason="follow_sell_pending")
    assert await exits.remaining_qty("e1:AAPL:long") == 0
    await exits.finalize_exit(exit_id, sold_qty=0, sold_avg_price=None,
                              broker_order_ref=None, reason="follow_sell_place_failed")
    assert await exits.remaining_qty("e1:AAPL:long") == 100
    assert await exits.sold_qty_for_intent("e1:AAPL:long") == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/integration/test_position_exit_store.py -p no:cacheprovider -q`
Expected: FAIL — `AttributeError: 'PositionExitStore' object has no attribute 'reserve_exit'`.

- [ ] **Step 3: Add `reserve_exit` and `finalize_exit`**

In `infra/storage/position_exit_store.py`, insert these two methods immediately after `record_exit` (after its closing `await self._conn.commit()` on line ~58, before `sold_qty_for_intent`):

```python
    async def reserve_exit(self, *, fingerprint: str, event_id: str | None,
                           intent_id: str, channel: str | None, ticker: str | None,
                           scope: str, requested_qty: int, reason: str | None) -> int:
        """Reserve an in-flight trader-sell of `requested_qty` shares BEFORE the
        order is placed. The row carries sold_qty=NULL (a pending reservation);
        remaining_qty counts requested_qty for it, so a concurrent trim (or a
        second-fingerprint sell) cannot size against shares this sell is already
        taking. Mirrors the trim ladder's in-flight reserve. Returns the row id;
        finalize_exit(id, ...) corrects it to the actual fill."""
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.execute(
            "INSERT INTO position_exits "
            "(fingerprint, event_id, intent_id, channel, ticker, scope, "
            " requested_qty, sold_qty, sold_avg_price, broker_order_ref, reason, "
            " created_at) VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
            (fingerprint, event_id, intent_id, channel, ticker, scope,
             requested_qty, reason, now),
        )
        await self._conn.commit()
        return int(cur.lastrowid)

    async def finalize_exit(self, exit_id: int, *, sold_qty: int,
                            sold_avg_price: float | None,
                            broker_order_ref: str | None,
                            reason: str | None) -> None:
        """Finalize a reserved exit with the ACTUAL fill. Setting sold_qty (even
        0) converts the pending reserve into a recorded exit: remaining_qty then
        counts the real sold_qty and releases any over-reserve (requested-sold).
        A finalize with sold_qty=0 fully releases the reserve (nothing sold)."""
        await self._conn.execute(
            "UPDATE position_exits "
            "SET sold_qty=?, sold_avg_price=?, broker_order_ref=?, reason=? "
            "WHERE id=?",
            (sold_qty, sold_avg_price, broker_order_ref, reason, exit_id),
        )
        await self._conn.commit()
```

- [ ] **Step 4: Change `remaining_qty`'s exit term to count reserves**

In `infra/storage/position_exit_store.py`, in `remaining_qty`, replace these two lines (currently the last lines of the method):

```python
        exits_sold = await self.sold_qty_for_intent(intent_id)
        return max(0, fill_qty - trims_sold - exits_sold)
```

with:

```python
        # Exits net BOTH recorded sells (sold_qty) AND in-flight reserves
        # (sold_qty NULL -> reserve requested_qty), symmetric to the trim reserve
        # above. This closes the trim/sell race: while a trader-sell is placed-
        # but-unrecorded, remaining_qty already excludes the shares it is taking.
        async with self._conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN sold_qty IS NOT NULL THEN sold_qty "
            "ELSE COALESCE(requested_qty, 0) END), 0) "
            "FROM position_exits WHERE intent_id=?",
            (intent_id,),
        ) as cur:
            row = await cur.fetchone()
        exits_reserved = int(row[0] or 0)
        return max(0, fill_qty - trims_sold - exits_reserved)
```

Also update the `remaining_qty` docstring's first line to mention exits net reserves too. Change:

```python
        """Shares still held for an intent = fill_qty − trims − exits.
```

to:

```python
        """Shares still held for an intent = fill_qty − trims − exits, where
        BOTH trims and exits net recorded sells AND in-flight reserves (a claimed
        trim rung, or a placed-but-unrecorded trader-sell).
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/integration/test_position_exit_store.py -p no:cacheprovider -q`
Expected: PASS (all tests in the file, including the 3 new ones).

- [ ] **Step 6: Run the full suite (must stay green; §3a still xfails)**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest -p no:cacheprovider -q`
Expected: `... passed, 1 xfailed` — §3a still xfails because the follower still uses `record_exit` (no reserve created yet), so the oversell still occurs. The new store methods are inert until Task 2 wires them.

- [ ] **Step 7: Commit**

```bash
cd /Users/jasonli/dev/trading-agent
git add infra/storage/position_exit_store.py tests/integration/test_position_exit_store.py
git commit -m "feat(storage): reserve_exit/finalize_exit; count in-flight sell reserve in remaining_qty"
```

---

### Task 2: Follower — reserve before place, finalize after fill; close §3a

**Files:**
- Modify: `skills/execution/sell_follower.py:22-56` (`follow_sell_position`)
- Test: `tests/integration/test_sell_follower.py`
- Modify: `tests/property/test_inflight_sell_oversell.py` (remove `xfail` marker)

**Interfaces:**
- Consumes: `reserve_exit` / `finalize_exit` from Task 1; existing imports in `sell_follower.py` (`IBGatewayUnavailable`, `PreparedOrder`, `FillStatus`, `marketable_sell_limit`, `logger`).
- Produces: `follow_sell_position(...)` with unchanged signature and return semantics (returns actual `sold`); end-state in `position_exits` is one finalized row per call, identical to before.

- [ ] **Step 1: Import `follow_sell_position` into the sell-follower test**

In `tests/integration/test_sell_follower.py`, change line 9 from:

```python
from skills.execution.sell_follower import SellFollower
```

to:

```python
from skills.execution.sell_follower import SellFollower, follow_sell_position
```

- [ ] **Step 2: Write the failing reserve-before-place test**

In `tests/integration/test_sell_follower.py`, append at the end of the file:

```python
@pytest.mark.asyncio
async def test_reserve_is_written_before_place_order(db):
    # The in-flight sell reserve must exist BEFORE place_order returns, so a
    # concurrent reader (the trim ladder, mid-wait_fill) sees remaining_qty net
    # of this sell and cannot oversize. This is the §3a fix.
    intents = TradeIntentStore(db)
    exits = PositionExitStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=100))
    gw = _gw()
    observed = {}

    async def place(contract, order, coid):
        observed["remaining_at_place"] = await exits.remaining_qty("e1:AAPL:long")
        return MagicMock()
    gw.place_order = AsyncMock(side_effect=place)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=100, remaining_qty=0, avg_fill_price=99.5,
        last_status="Filled", status_timestamp=_now()))

    sold = await follow_sell_position(
        gw=gw, exits_store=exits, fingerprint="fp", event_id="e",
        intent_id="e1:AAPL:long", channel="mystic", ticker="AAPL", qty=100,
        scope="full", slippage_cap_pct=0.01, fill_timeout=5.0)

    assert sold == 100
    # Reserve already counted while the order was being placed (100 - 100 = 0).
    assert observed["remaining_at_place"] == 0
    # End state: the reserve is finalized to the actual fill.
    assert await exits.remaining_qty("e1:AAPL:long") == 0
```

- [ ] **Step 3: Run the new test to verify it fails**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest "tests/integration/test_sell_follower.py::test_reserve_is_written_before_place_order" -p no:cacheprovider -q`
Expected: FAIL — `assert 100 == 0` (current code calls `record_exit` only AFTER `wait_fill`, so `remaining_qty` is still 100 when `place_order` runs).

- [ ] **Step 4: Rewrite `follow_sell_position`**

In `skills/execution/sell_follower.py`, replace the entire function body of `follow_sell_position` (lines 22-56, from `async def follow_sell_position(` through `return sold`) with:

```python
async def follow_sell_position(
    *, gw, exits_store, fingerprint: str, event_id: str | None, intent_id: str,
    channel: str | None, ticker: str, qty: int, scope: str,
    slippage_cap_pct: float, fill_timeout: float,
) -> int:
    """Submit a marketable-limit SELL for `qty` shares of one position and record
    the exit. Returns the actual quantity sold (0 on a non-fill). Mirrors the
    trim ladder's partial-fill discipline: cancel any residual, record the real
    fill. Raises IBGatewayUnavailable to the caller (which owns the claim).

    Reserves the full `qty` in the exit ledger BEFORE placing the order, so a
    concurrent trim (or a second-fingerprint sell) reads remaining_qty net of
    this in-flight sell and cannot oversize (spec §3a). The reserve is corrected
    to the actual fill once known, symmetric to the trim ladder's in-flight
    reserve."""
    contract = await gw.qualify_equity(ticker)
    price = await gw.get_quote(ticker)
    limit = marketable_sell_limit(price, slippage_cap_pct)
    order = PreparedOrder(action="SELL", quantity=qty, order_type="LMT",
                          limit_price=limit, tif="DAY")
    client_order_id = f"{intent_id}:exit:{fingerprint[:16]}"

    # Reserve BEFORE placing: remaining_qty now excludes these shares for any
    # concurrent reader (trim ladder / second sell) for the whole place->fill
    # window.
    exit_id = await exits_store.reserve_exit(
        fingerprint=fingerprint, event_id=event_id, intent_id=intent_id,
        channel=channel, ticker=ticker, scope=scope, requested_qty=qty,
        reason="follow_sell_pending")
    try:
        trade = await gw.place_order(contract, order, client_order_id)
    except IBGatewayUnavailable:
        # Nothing was placed -> release the reserve so the caller's retry can
        # re-size against the still-held shares; re-raise (the caller owns the
        # claim / retry decision).
        await exits_store.finalize_exit(
            exit_id, sold_qty=0, sold_avg_price=None, broker_order_ref=None,
            reason="follow_sell_place_failed")
        raise
    # If wait_fill raises, the order may be live at IB: leave the reserve in
    # place (stuck-until-reconciled), which is the safe direction -- it can
    # never oversell.
    fill = await gw.wait_fill(trade, timeout=fill_timeout)

    sold = int(fill.filled_qty) if fill.filled_qty and fill.filled_qty > 0 else 0
    if fill.status != FillStatus.FILLED:
        # Cancel any residual working order (zero-fill or partial).
        try:
            await gw.cancel_order(trade)
        except Exception:
            logger.exception("sell residual cancel failed (order may rest at IB)")

    # Finalize the reserve to the actual fill: releases any over-reserve
    # (requested-sold); sold=0 releases the whole reserve.
    await exits_store.finalize_exit(
        exit_id, sold_qty=sold, sold_avg_price=fill.avg_fill_price,
        broker_order_ref=fill.broker_order_id, reason="follow_sell")
    if sold < qty:
        logger.warning("follow_sell %s: sold %d/%d (%s)", intent_id, sold, qty,
                       fill.last_status)
    return sold
```

- [ ] **Step 5: Run the new test + the whole sell-follower file**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/integration/test_sell_follower.py -p no:cacheprovider -q`
Expected: PASS (the new timing test + all pre-existing follower tests — their end-state assertions are unchanged: a full fill finalizes to `sold_qty=req`, zero-fill/broker-down finalize to `sold_qty=0`).

- [ ] **Step 6: Remove the §3a `xfail` marker (same commit — see "Why" note above)**

In `tests/property/test_inflight_sell_oversell.py`, delete the three-line decorator (lines 18-20):

```python
@pytest.mark.xfail(strict=True, reason="FINDING: in-flight trader-sell is "
    "unreserved in remaining_qty; a concurrent trim oversells (recorded 150 of "
    "100). See spec 2026-06-26 §3a. Fix (reserve in-flight sells) pending sign-off.")
```

Leave the `@pytest.mark.asyncio` decorator and the rest of the test as-is. Then update the module docstring (lines 1-4) to reflect that the bug is now fixed; replace it with:

```python
"""§3a regression: a trim firing inside a trader-sell's in-flight (placed but
unrecorded) window must NOT oversell. follow_sell_position reserves the full
sell qty before place_order, so remaining_qty=0 during the window and the
concurrent trim short-circuits. Guards the fix in spec 2026-06-26 §3a.
"""
```

- [ ] **Step 7: Run the §3a probe — now a passing regression**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/property/test_inflight_sell_oversell.py -p no:cacheprovider -q`
Expected: `1 passed` (no longer xfailed/xpassed). `total_recorded == 100 <= 100`.

- [ ] **Step 8: Run the full suite (green, 0 xfailed)**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest -p no:cacheprovider -q`
Expected: all passed, `0 xfailed` (the 1 xfail is gone; +1 new follower test).

- [ ] **Step 9: Commit**

```bash
cd /Users/jasonli/dev/trading-agent
git add skills/execution/sell_follower.py tests/integration/test_sell_follower.py tests/property/test_inflight_sell_oversell.py
git commit -m "fix(execution): reserve in-flight trader-sell before place; close §3a oversell"
```

---

### Task 3: Consumers — exclude pending reserves from P&L; guard exposure

**Files:**
- Modify: `bin/pnl_report.py:61-66` (the exits query in `_fetch`)
- Test: `tests/integration/test_pnl_report_cli.py`
- Test: `tests/unit/test_exposure.py`

**Interfaces:**
- Consumes: `reserve_exit` (Task 1); `cli._fetch` (module-level function in `bin/pnl_report.py`); `open_deployed_notional` (already imported in the exposure test).
- Produces: `_fetch` returns an `exits` list that excludes pending (`sold_qty IS NULL`) reserves.

**Note on placement:** the spec §5 lists the P&L-exclusion check under `test_position_exit_store.py`, but the query lives in `bin/pnl_report.py::_fetch`. Testing it where it runs (`test_pnl_report_cli.py`, which already imports `bin.pnl_report as cli`) is the faithful placement and exercises the real query. Exposure needs no production change (its `SUM(e.sold_qty)` already ignores NULL); we add a guard test documenting the intentional asymmetry.

- [ ] **Step 1: Write the failing P&L `_fetch` test**

In `tests/integration/test_pnl_report_cli.py`, append at the end of the file:

```python
# ---------------------------------------------------------------------------
# In-flight (pending) exit reserves must not reach the realized report.
# ---------------------------------------------------------------------------

async def _seed_pending_and_finalized(db_path):
    """Two filled lots. 'done' has a finalized exit (sold_qty=10); 'pend' has
    only an in-flight reserve (sold_qty NULL). _fetch must keep 'done' and drop
    'pend' so a pending reserve never reaches compute_attribution or the
    since-sell window logic as phantom proceeds."""
    conn = await get_connection(db_path)
    intents = TradeIntentStore(conn)
    exits = PositionExitStore(conn)
    base = {
        "event_id": "e", "channel": "stp", "side": "long",
        "instrument_type": "equity", "conviction": "high",
        "execution_state": "filled", "outbox_status": "confirmed",
        "policy_state": "approved",
        "signal_received_at": "2026-05-01T00:00:00+00:00",
        "intent_created_at": "2026-05-01T00:00:00+00:00",
        "filled_at": "2026-05-01T14:00:00+00:00",
        "created_at": "2026-05-01T00:00:00+00:00",
        "updated_at": "2026-05-01T00:00:00+00:00",
    }
    await intents.insert({**base, "intent_id": "done", "ticker": "NVDA",
                          "fill_price": 100.0, "fill_qty": 10})
    await intents.insert({**base, "intent_id": "pend", "ticker": "TSLA",
                          "fill_price": 50.0, "fill_qty": 10})
    await exits.record_exit(fingerprint="f1", event_id="e", intent_id="done",
                            channel="stp", ticker="NVDA", scope="full",
                            requested_qty=10, sold_qty=10, sold_avg_price=110.0,
                            broker_order_ref="r1", reason="follow_sell")
    await exits.reserve_exit(fingerprint="f2", event_id="e", intent_id="pend",
                             channel="stp", ticker="TSLA", scope="full",
                             requested_qty=10, reason="follow_sell_pending")
    await conn.close()


@pytest.fixture
def pending_finalized_db(tmp_path):
    db_path = str(tmp_path / "pendfin.db")
    asyncio.run(_seed_pending_and_finalized(db_path))
    return db_path


def test_fetch_excludes_pending_reserves_keeps_finalized(pending_finalized_db):
    entries, trims, exits = cli._fetch(pending_finalized_db)
    exit_ids = {e["intent_id"] for e in exits}
    assert "done" in exit_ids      # finalized exit retained
    assert "pend" not in exit_ids  # in-flight reserve dropped
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest "tests/integration/test_pnl_report_cli.py::test_fetch_excludes_pending_reserves_keeps_finalized" -p no:cacheprovider -q`
Expected: FAIL — `assert 'pend' not in {'done', 'pend'}` (without the filter, `_fetch` returns the pending row).

- [ ] **Step 3: Add the `WHERE sold_qty IS NOT NULL` filter**

In `bin/pnl_report.py`, in `_fetch`, change the exits query (currently lines 61-66):

```python
        exits = [r for r in _safe_fetchall(
            conn,
            "SELECT intent_id, sold_qty, sold_avg_price, created_at "
            "FROM position_exits")
            if r["intent_id"] in ids
            and (not since_sell or (r["created_at"] or "") >= since_sell)]
```

to:

```python
        # `WHERE sold_qty IS NOT NULL` drops in-flight (pending) sell reserves so
        # they never reach compute_attribution or the since-sell window as
        # phantom proceeds. Finalized exits (incl. sold_qty=0 zero-fills) stay.
        exits = [r for r in _safe_fetchall(
            conn,
            "SELECT intent_id, sold_qty, sold_avg_price, created_at "
            "FROM position_exits WHERE sold_qty IS NOT NULL")
            if r["intent_id"] in ids
            and (not since_sell or (r["created_at"] or "") >= since_sell)]
```

- [ ] **Step 4: Run the P&L test + the whole P&L file**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/integration/test_pnl_report_cli.py -p no:cacheprovider -q`
Expected: PASS (the new test + all pre-existing P&L tests; the pre-existing `sold_qty=0` zero-fill row stays included because `0 IS NOT NULL`, so those tests are unaffected).

- [ ] **Step 5: Add the exposure guard test**

In `tests/unit/test_exposure.py`, append at the end of the file:

```python
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
```

This test passes on first run (exposure has no production change); it characterizes/guards the intentional asymmetry.

- [ ] **Step 6: Run the exposure file**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/unit/test_exposure.py -p no:cacheprovider -q`
Expected: PASS (all, including the new guard).

- [ ] **Step 7: Commit**

```bash
cd /Users/jasonli/dev/trading-agent
git add bin/pnl_report.py tests/integration/test_pnl_report_cli.py tests/unit/test_exposure.py
git commit -m "fix(pnl): drop pending (sold_qty NULL) exit reserves from realized report"
```

---

### Task 4: Property suite — randomized N3 in-flight-sell rule + faithful oracle

**Files:**
- Modify: `tests/property/test_position_invariants.py`

**Interfaces:**
- Consumes: `follow_sell_position` and `fire_rung_if_crossed` (both will run against the live fix); the machine's existing `self.gw` (FakeGateway with the `on_wait_fill` one-shot hook), `self.exits`, `self.trims`, `self._fill`, `self._rungs`, `self._positive_fires`, `self._seq`.
- Produces: a new `@rule` exercising a sell whose `on_wait_fill` fires a real trim; an `_exit_contribution` helper keeping `remaining_qty_identity` faithful to the new exit term.

- [ ] **Step 1: Import `follow_sell_position`**

In `tests/property/test_position_invariants.py`, change line 22 from:

```python
from skills.execution.sell_follower import SellFollower
```

to:

```python
from skills.execution.sell_follower import SellFollower, follow_sell_position
```

- [ ] **Step 2: Add the N3 rule**

In `tests/property/test_position_invariants.py`, add this rule inside `PositionInvariantMachine`, immediately after the `follow_sell` rule's `_positive_exit_count` helper (after line ~170, before `@invariant() def claim_once_idempotency`):

```python
    @rule(intent=intents, rung=st.sampled_from([1, 2, 3]),
          fp_key=st.integers(min_value=0, max_value=3))
    def inflight_sell_with_concurrent_trim(self, intent, rung, fp_key):
        """N3: while a trader-sell is placed-but-unrecorded (inside wait_fill),
        fire a REAL trim rung against the REAL remaining_qty. The in-flight sell
        reserve must make remaining_qty exclude the shares this sell is taking,
        so the concurrent trim short-circuits and cannot oversize. The
        never_oversell / remaining_qty_identity invariants then verify it.
        (Randomized generalization of the §3a probe.)"""
        meta = self._rungs[intent].get(rung)
        if meta is None or meta["recorded"]:
            return  # rung not armed, already fired, or stuck-claimed
        rem = self._run(self.exits.remaining_qty(intent))
        if rem <= 0:
            return  # nothing to sell -> no in-flight window to exercise
        channel = self._rungs[intent]["_meta"]["channel"]
        ticker = self._rungs[intent]["_meta"]["ticker"]
        fill_qty = self._fill[intent]
        self._seq += 1
        fingerprint = f"inflight-{channel}-{ticker}-{fp_key}-{self._seq}"

        async def concurrent_trim():
            # current_price deterministically crosses this rung's threshold.
            price = 100.0 * (1.0 + meta["threshold_pct"]) + 1.0
            fired = await fire_rung_if_crossed(
                gw=self.gw, trim_store=self.trims, exits_store=self.exits,
                intent_id=intent, ticker=ticker, avg_fill_price=100.0,
                original_qty=fill_qty, rung=rung,
                threshold_pct=meta["threshold_pct"], trim_pct=meta["trim_pct"],
                current_price=price, slippage_cap_pct=0.01)
            if fired:
                meta["recorded"] = True
                key = (intent, rung)
                self._positive_fires[key] = self._positive_fires.get(key, 0) + 1

        self.gw.fill_mode = "full"
        self.gw.unavailable = False
        self.gw.on_wait_fill = concurrent_trim
        try:
            self._run(follow_sell_position(
                gw=self.gw, exits_store=self.exits, fingerprint=fingerprint,
                event_id=f"evt-{fingerprint}", intent_id=intent, channel=channel,
                ticker=ticker, qty=rem, scope="full", slippage_cap_pct=0.01,
                fill_timeout=5.0))
        finally:
            self.gw.on_wait_fill = None
            self.gw.unavailable = False
```

- [ ] **Step 3: Add the `_exit_contribution` helper**

In `tests/property/test_position_invariants.py`, add this method in the `# helpers` section, immediately after `_recorded_trims` (after line ~214, before the `# oracle` comment):

```python
    async def _exit_contribution(self, intent_id: str) -> tuple[int, int]:
        """Mirror remaining_qty's exit term from first principles: a finalized
        exit (sold_qty set) contributes sold_qty; an in-flight reserve (sold_qty
        NULL) contributes requested_qty."""
        recorded = reserved = 0
        async with self._conn.execute(
            "SELECT sold_qty, requested_qty FROM position_exits WHERE intent_id=?",
            (intent_id,),
        ) as cur:
            for sold_qty, requested_qty in await cur.fetchall():
                if sold_qty is not None:
                    recorded += int(sold_qty)
                else:
                    reserved += int(requested_qty or 0)
        return recorded, reserved
```

- [ ] **Step 4: Make `remaining_qty_identity` count exit reserves**

In `tests/property/test_position_invariants.py`, replace the body of `remaining_qty_identity` (the loop, currently lines ~219-227) with:

```python
        for intent_id, fill_qty in self._fill.items():
            rem = self._run(self.exits.remaining_qty(intent_id))
            rec_trims, trim_reserves = self._run(self._recorded_trims(intent_id))
            rec_exits, exit_reserves = self._run(self._exit_contribution(intent_id))
            expected = max(0, fill_qty - rec_trims - trim_reserves
                           - rec_exits - exit_reserves)
            assert rem == expected, (
                f"{intent_id}: remaining_qty={rem} != {expected} "
                f"(fill={fill_qty} trims={rec_trims} trim_reserves={trim_reserves} "
                f"exits={rec_exits} exit_reserves={exit_reserves})")
            assert rem >= 0, f"{intent_id}: negative remaining {rem}"
```

(Leave `never_oversell` unchanged — it counts only RECORDED sells via `sold_qty_for_intent`, which is correct.)

- [ ] **Step 5: Run the property suite (dev profile)**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/property/ -p no:cacheprovider -q`
Expected: PASS. The N3 rule is exercised under Hypothesis (dev: 50 examples, 24 steps); the fix makes the concurrent trim short-circuit, so `never_oversell` holds. (Without the Task-2 fix this rule would oversell — it is the randomized guard.)

- [ ] **Step 6: Run the property suite under the CI profile (spec §7: dev + ci)**

Run: `cd /Users/jasonli/dev/trading-agent && HYPOTHESIS_PROFILE=ci uv run --extra dev pytest tests/property/ -p no:cacheprovider -q`
Expected: PASS (ci: 300 examples, 40 steps, non-derandomized — broader exploration).

- [ ] **Step 7: Commit**

```bash
cd /Users/jasonli/dev/trading-agent
git add tests/property/test_position_invariants.py
git commit -m "test(property): N3 in-flight-sell rule; keep remaining_qty oracle faithful"
```

---

### Task 5: Final verification (no code change)

**Files:** none.

- [ ] **Step 1: Full suite, dev profile**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest -p no:cacheprovider -q`
Expected: all passed, `0 xfailed`. Compared to baseline (`516 passed, 1 xfailed`): the §3a xfail is gone and now passes; +4 new tests (3 store + 1 follower) + 1 P&L + 1 exposure + 1 property rule does not add a separate test id (rules run inside the existing stateful TestCase). Net: ≈ `522 passed, 0 xfailed` (exact count may differ slightly; the invariant is 0 xfailed and 0 failures).

- [ ] **Step 2: Confirm the §3a success criteria (spec §7)**

Run: `cd /Users/jasonli/dev/trading-agent && uv run --extra dev pytest tests/property/test_inflight_sell_oversell.py tests/property/test_position_invariants.py tests/integration/test_sell_follower.py tests/integration/test_position_exit_store.py tests/integration/test_pnl_report_cli.py tests/unit/test_exposure.py -p no:cacheprovider -v 2>&1 | tail -40`
Expected: `test_inflight_sell_concurrent_trim_does_not_oversell PASSED`; all listed files green.

- [ ] **Step 3: Push the branch and open a PR (only if the operator asks)**

The branch is `fix/inflight-sell-reserve`. Because PR #14 (`feat/money-safety-invariant-suite`) is still open and is this branch's base, the PR should target `feat/money-safety-invariant-suite` (or be rebased onto `master` after #14 merges). Do NOT push/PR without explicit confirmation.

---

## Self-Review

**1. Spec coverage:**
- §3.1 `reserve_exit` / `finalize_exit` / reserve-aware `remaining_qty` / keep `record_exit` + `sold_qty_for_intent` → Task 1. ✔
- §3.2 follower reserve-before-place, finalize-after-fill → Task 2. ✔
- §3.3 lifecycle/error matrix (place raises → finalize 0 release+reraise; wait_fill raises → keep; zero/partial/full → finalize sold; crash → stuck) → encoded in Task 2 function + Global Constraints table; verified by `test_finalize_exit_zero_releases_full_reserve` (Task 1) and pre-existing broker-down/zero-fill follower tests (Task 2 Step 5). ✔
- §3.4 P&L `WHERE sold_qty IS NOT NULL` (one line) → Task 3; exposure NO change + guard → Task 3. ✔
- §4 why it closes §3a (reserve → remaining_qty=0 → `exit_ladder.py:38` short-circuit) → Task 2 + §3a probe (Task 2 Step 7) + N3 (Task 4). ✔
- §5 tests: §3a marker removed (Task 2); N3 rule (Task 4); store unit coverage (Task 1); pending excluded from `sold_qty_for_intent` (Task 1) and from P&L query (Task 3); re-verify green for sell_follower / pnl_report_cli / exposure (Tasks 2/3 run the whole files). ✔
- §6 out of scope: no auto-cleanup, no migration, no extra fuzzing — honored. ✔
- §7 success criteria: §3a xfail→pass; full suite green; no oversell under randomized rule (dev+ci); P&L/exposure unaffected → Task 5 + Task 4 Steps 5-6. ✔

**2. Placeholder scan:** No TBD/“add error handling”/“similar to Task N”. Every code step shows complete code; every run step shows an exact command + expected output. ✔

**3. Type consistency:** `reserve_exit(...) -> int`; `finalize_exit(exit_id: int, *, sold_qty, sold_avg_price, broker_order_ref, reason) -> None` — used identically in Task 2 (`reason="follow_sell_pending"` at reserve, `"follow_sell_place_failed"`/`"follow_sell"` at finalize) and the Task 1/3 tests. `_exit_contribution(intent_id) -> tuple[int, int]` returns `(recorded, reserved)`, consumed positionally as `rec_exits, exit_reserves`. The exit-term SQL (`CASE WHEN sold_qty IS NOT NULL THEN sold_qty ELSE COALESCE(requested_qty,0)`) matches the oracle helper. ✔
