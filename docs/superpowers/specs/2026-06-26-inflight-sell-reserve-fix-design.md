# In-Flight Sell Reserve — §3a Oversell Fix — Design

- **Date:** 2026-06-26
- **Branch:** `fix/inflight-sell-reserve`, based on the property suite it fixes. The fix's tests depend on PR #14, so branch off `master` **after PR #14 merges**, or off `feat/money-safety-invariant-suite` while #14 is still open — NOT bare `master`.
- **Status:** Approved (design) — ready for `writing-plans` + implementation. **Recommended: implement in a fresh session** (production money-execution code; clean context).
- **Author:** Jason + Claude

## 0. Resume instructions (for a new session)

This design is approved. To execute: read this spec, then invoke `superpowers:writing-plans` → `superpowers:subagent-driven-development`. TDD is pre-seeded — the red test already exists: `tests/property/test_inflight_sell_oversell.py` is currently `xfail(strict=True)`; the fix flips it to a passing assertion. Related context: the property suite that found this bug (`docs/superpowers/specs/2026-06-26-money-safety-invariant-suite-design.md`, merged via PR #14) and memory `[[project_money_safety_invariant_suite]]`.

## 1. Context

The money-safety property suite (PR #14) CONFIRMED a real oversell (spec §3a): `PositionExitStore.remaining_qty` reserves in-flight **trims** (a rung with `fire_started_at` set, `fired_at` NULL) but has **no reserve for an in-flight trader-sell**. Because `SellFollower` and `ExitLadder` run concurrently on one event loop (`main.py:301`/`303`), a trim firing inside a sell's `place_order → wait_fill → record_exit` window reads stale `remaining_qty`, sizes a trim against shares the sell is already taking, and both record — **150 of 100 shares, leaving the account short 50.** Reproduced deterministically in `tests/property/test_inflight_sell_oversell.py`.

## 2. Goal

Make `remaining_qty` account for in-flight trader-sells, symmetric to the existing in-flight trim reserve, so a concurrent trim (or second-fingerprint sell) cannot oversize. Success = the §3a probe flips from `xfail` to a passing regression test, the full suite stays green, no production behavior regresses for P&L or exposure, and a new randomized in-flight-sell interleaving rule holds under Hypothesis.

## 3. Approach (A — pending exit row; no schema migration)

The `position_exits` table already has `requested_qty` (INTEGER) and a nullable `sold_qty` (INTEGER). Reuse them: a row with `sold_qty IS NULL` is an **in-flight reservation** of `requested_qty`; once finalized, `sold_qty` holds the actual sold amount. This mirrors `remaining_qty`'s existing trim precedence ("recorded `sold_qty` wins, else reserve").

### 3.1 `infra/storage/position_exit_store.py`
- **Add** `async def reserve_exit(*, fingerprint, event_id, intent_id, channel, ticker, scope, requested_qty, reason) -> int`: INSERT a row with `requested_qty=requested_qty`, `sold_qty=NULL`, `sold_avg_price=NULL`, `broker_order_ref=NULL`. Return the new row id (`cursor.lastrowid`).
- **Add** `async def finalize_exit(exit_id: int, *, sold_qty, sold_avg_price, broker_order_ref, reason) -> None`: `UPDATE position_exits SET sold_qty=?, sold_avg_price=?, broker_order_ref=?, reason=? WHERE id=?`.
- **Change** `remaining_qty`'s exit term. Currently: `exits_sold = await self.sold_qty_for_intent(intent_id)` (= `SUM(sold_qty)`). Replace the exit contribution with a query that counts the reserve for pending rows:
  `SUM(CASE WHEN sold_qty IS NOT NULL THEN sold_qty ELSE COALESCE(requested_qty, 0) END)` over `position_exits WHERE intent_id=?`.
  So `remaining = max(0, fill_qty − recordedTrims − inFlightTrimReserves − (recordedExits + inFlightSellReserves))`.
- **Keep** `record_exit` (still valid for any direct-record caller/test) and `sold_qty_for_intent` (= `SUM(sold_qty)`, the ACTUAL-sold total; used by P&L/exposure semantics — must NOT count reserves).

### 3.2 `skills/execution/sell_follower.py::follow_sell_position`
Reserve before placing; finalize after the fill replaces the current single `record_exit`:
```
exit_id = await exits_store.reserve_exit(... requested_qty=qty, reason="follow_sell_pending")
try:
    trade = await gw.place_order(contract, order, client_order_id)
except IBGatewayUnavailable:
    await exits_store.finalize_exit(exit_id, sold_qty=0, sold_avg_price=None,
                                    broker_order_ref=None, reason="follow_sell_place_failed")
    raise                      # nothing placed -> release the reserve; caller retries
fill = await gw.wait_fill(trade, timeout=fill_timeout)   # if THIS raises, reserve stays (order may be live) -> stuck, safe
... residual cancel unchanged ...
await exits_store.finalize_exit(exit_id, sold_qty=sold, sold_avg_price=fill.avg_fill_price,
                                broker_order_ref=fill.broker_order_id, reason="follow_sell")
```

### 3.3 Lifecycle / error matrix
| Event | Reserve disposition |
|---|---|
| `place_order` raises (nothing placed) | finalize `sold_qty=0` → **release**; re-raise (retry-safe) |
| `wait_fill` raises (order may be live at IB) | **keep** reserve → stuck-until-reconciled (safe) |
| zero-fill | finalize `sold_qty=0` → release |
| partial fill (N<req) | finalize `sold_qty=N` → release the over-reserved `req−N` |
| full fill | finalize `sold_qty=req` |
| crash between reserve and finalize | stuck reserve (safe; never oversells); accepted, no auto-cleanup |

### 3.4 P&L / exposure
- **`bin/pnl_report.py:62-64`** — the exits query (`SELECT intent_id, sold_qty, sold_avg_price, created_at FROM position_exits`) must add **`WHERE sold_qty IS NOT NULL`** so pending reserves never reach `compute_attribution` as phantom proceeds. (One-line change.)
- **`skills/risk/exposure.py:21`** — `SUM(e.sold_qty)` already ignores NULL pending rows, which is **correct** (the shares are still held until the sell finalizes). **No change.**

## 4. Why this closes §3a
With the reserve written before `place_order`, a concurrent `fire_rung_if_crossed` reads `remaining_qty = 0` during the sell's window and short-circuits at `agent/exit_ladder.py:38` (`remaining_held <= 0 → return False`). Recorded total stays ≤ `fill_qty`.

## 5. Tests
- **`tests/property/test_inflight_sell_oversell.py`** — remove the `xfail(strict=True)` marker; the assertion `total_recorded <= fill_qty` now **passes**.
- **`tests/property/test_position_invariants.py`** — add a randomized **in-flight-sell rule** (N3): a rule that runs a sell whose `FakeGateway.on_wait_fill` fires a trim (real `fire_rung_if_crossed`) mid-window, asserting the existing oracle (never-oversell) still holds. Guards the fix under randomization, not just the one scenario.
- **`tests/integration/test_position_exit_store.py`** — unit coverage: `reserve_exit` creates a pending row that `remaining_qty` reserves (counts `requested_qty`); `finalize_exit` corrects it to actual and releases over-reserve; a pending row is excluded from `sold_qty_for_intent` and from the P&L exits query.
- **Re-verify green:** `tests/integration/test_sell_follower.py`, `tests/integration/test_pnl_report_cli.py`, `tests/unit/test_exposure*.py` — end-state behavior is unchanged (full fills finalize to the same `sold_qty`; zero-fill/broker-down paths behave as before).

## 6. Out of scope
- Auto-cleanup / reconciler handling of stuck sell reservations (accepted stuck-until-reconciled, consistent with trims).
- Any schema migration (the `requested_qty`/`sold_qty` columns already exist).
- Broader in-flight-sell concurrency fuzzing beyond the N3 rule.

## 7. Success criteria
- §3a probe: `xfail` → **pass**.
- Full suite green (the count drops one xfail and gains the passing probe + new tests).
- No oversell under the randomized in-flight-sell rule (Hypothesis, dev + ci).
- `bin/pnl_report.py` and exposure unaffected by pending reserves (verified by the unit tests above).
