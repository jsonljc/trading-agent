# Money-Safety Invariant Suite — Design

- **Date:** 2026-06-26
- **Branch:** fix/north-star-hardening
- **Status:** Confirmed — seams verified, async spike green, invariants audited. Proceeding to implementation plan.
- **Author:** Jason + Claude

## 0. Confirmation results (2026-06-26)

Three parallel verification agents ran before this revision:
- **Seams:** every store/skill/model signature this spec relies on is confirmed at
  file:line. Account summary is `AccountSummary(net_liquidation: float,
  buying_power: float, currency: str)` (`infra/ib/models.py:54`). No discrepancies.
- **Async spike:** the Hypothesis-over-async pattern works (recipe in §8); a planted
  oversell was caught and shrank to a 2-step counterexample. `hypothesis 6.155.7`
  installed into `.venv`.
- **Invariant audit:** refined the oracle (caveats folded into §3) and surfaced a
  plausible **real** oversell bug — the in-flight trader-sell is unreserved (§3a).

## 1. Context & Motivation

The trading agent auto-executes buy/sell orders against Interactive Brokers from
Discord signals, with no human in the loop. The branch is hardening toward
go-live (gateway currently PAPER-LOCKED). The suite is green today —
**498 tests pass in ~4.3s**, fully deterministic (in-memory SQLite, no live
LLM/broker).

Those tests are **example-based**: each drives one scripted scenario
(`test_sell_follower.py`, `test_trim_ladder_store.py`, `test_position_exit_store.py`,
`test_order_sizer*.py`). They cover happy paths and obvious edges well. The
**gap** is *interleaved* sequences — a price-trigger trim firing at the same
moment a trader's sell arrives, a "sold" message redelivered twice, a crash
between deciding to sell and recording the sale. That is exactly where the
money-losing bugs hide, and exactly what the production code works hardest to
prevent (atomic claims, in-flight reserves, claim-once idempotency).

This spec adds a **property-based, stateful invariant suite** that plays out
thousands of randomized realistic sequences and, after every step, asserts a
small set of rules that must never break. If any sequence can break a rule,
Hypothesis shrinks it to a minimal reproducible counterexample.

## 2. Goals / Non-Goals

**Goals**
- Stress the real money-safety code (real stores + real `SellFollower`) under
  randomized, interleaved operation sequences — faking only the broker.
- Encode the catastrophic invariants as machine-checked properties.
- Stay deterministic, offline, and fast enough to run in the normal `pytest`
  suite and in CI.
- Produce either *evidence the invariants hold* pre-go-live, or a *minimal
  reproducible counterexample* (a real finding) — starting with §3a.

**Non-Goals (deferred to separate specs)**
- Options-sleeve invariants (shares path only for v1).
- LLM confidence-calibration and live-LLM drift evals (different subsystem,
  different cadence, costs API calls).
- Historical-price economic backtest.
- True multi-threaded concurrency — modeled here via deterministic
  *interleaving* of split begin/commit rules, not OS threads.

## 3. The Invariants (the oracle)

Checked after **every** operation. Refinements below come from the adversarial
audit of the real code — they prevent false-positive findings and must be encoded
in the oracle exactly as stated. **The oracle nets only *recorded* `sold_qty`
(never in-flight reserves) when summing sales.**

- **INV-1 — Never oversell (per intent):** `Σ(recorded trim sold_qty) +
  Σ(recorded exit sold_qty) ≤ fill_qty`. Holds for any **run-to-completion**
  ordering (each high-level op records before the next begins), because every
  recording path clamps to a fresh `remaining_qty`
  (`alloc = min(rem, fresh, target)`; `trim_qty = min(reserve, remaining_held)`).
  **Caveat / candidate finding:** `remaining_qty` reserves in-flight *trims* but
  **not** an in-flight *trader-sell* (the exit is recorded only after
  `wait_fill`). See §3a — the suite deliberately probes this.

- **INV-2 — Claim-once idempotency:** **at most one invocation records a
  `position_exits` row with `sold_qty > 0` per fingerprint.** (Not "at most one
  row": zero-fill lots legitimately write `sold_qty=0` rows, and a
  released-then-retried fingerprint may have such rows from >1 invocation.)
  Equivalent guarded form: `release_sell_event(fp)` is only ever called when
  `Σ position_exits.sold_qty WHERE fingerprint=fp == 0`.

- **INV-3 — No trim double-fire:** **per claim/release cycle** — no two
  *successful* `claim_for_fire` calls on a rung without an intervening
  `release_claim`, and **at most one positive-fill `record_fire` per rung ever.**
  Exercise through `fire_rung_if_crossed`, **not** raw `record_fire` (the latter
  is an unconditional UPDATE that overwrites rather than double-counts).

- **INV-4 — Sizing caps:** gate every assertion on a **genuine sizing success**,
  identified by `"quantity" in result.updates` — **not** `status == "success"`
  (which `partial_or`/`already_terminated` also return, without a `quantity`, when
  the shares leg already filled — even on exposure-cap-exceeded). For a genuine
  sizing success: (a) `quantity ≥ 1` always; (b) `quantity·unit_cost ≤
  buying_power` **only when** the account exposes a non-None `buying_power`
  (always true for the real `AccountSummary`); (c) `open_exposure_input +
  notional_estimate ≤ aggregate_notional_cap` **only when** both ctx keys were
  present at entry — using the **input** `open_exposure` (OrderSizer overwrites it
  in `updates`). Use a small float tolerance.

- **CONSISTENCY — Remaining-qty identity:** `remaining_qty(intent) == max(0,
  fill_qty − recordedTrims − recordedExits − inFlightTrimReserves)`, mirroring
  exactly: (1) **recorded fill wins over reserve** (a rung with non-NULL
  `sold_qty` counts the recorded value, never the reserve); (2) reserve rounding
  is `round_half_up_min1(fill_qty·trim_pct)` = `max(1, floor(n+0.5))` — not
  `round()` (banker's) or `floor()`; (3) the `max(0, …)` clamp is load-bearing —
  `recordedTrims + recordedExits + reserves` **can** legitimately exceed
  `fill_qty`, so never infer a *recorded* oversell from `remaining_qty == 0`.

**Design choice — assert invariants, not a reimplementation.** The oracle checks
inequalities and idempotency (properties that hold regardless of internal
allocation order), never re-implements `SellFollower`'s allocation.

### 3a. The INV-1 concurrency finding (candidate)

Static audit identified a plausible **real** oversell. With one position
(fill=100) and an armed trim rung:
1. `SellFollower` reads `remaining=100`, places SELL 100, suspends at `wait_fill`.
2. An `ExitLadder` tick reads `remaining_qty` (still 100 — the sell is unrecorded
   and *unreserved*), claims the rung, places SELL 50.
3. Both fill → `record_exit` 100 and `record_fire` 50 → **recorded 150 > 100.**

Reachable because the sell path and the exit ladder run **concurrently on the same
event loop** (`main.py:301`/`303`) and `remaining_qty` has no in-flight *sell*
reserve (only trim reserves). The suite **confirms or refutes this deterministically**
by modeling the in-flight-sell window as an explicit `begin_sell` / `complete_sell`
split (mirroring the trim claim/commit split). If confirmed, it is reported as a
finding per §10 — proposed fix: reserve in-flight sells in `remaining_qty`,
symmetric to trims — **not** worked around in the test.

## 4. Approach & Rejected Alternatives

**Chosen:** real stores + real `SellFollower` + real `OrderSizer` + a deterministic
`FakeGateway` + a lightweight Python shadow model. The genuine SQL atomic claims,
`remaining_qty()` netting, and `SellFollower.run()` orchestration are exercised;
only the broker is faked.

| Rejected | Why |
|---|---|
| Pure-logic (extract arithmetic, test functions) | Skips the atomic-claim/race logic — the entire point. |
| Full pipeline through `Orchestrator` | Too much surface, nondeterministic clocks, duplicates e2e coverage. |

## 5. Architecture — Components & Seams

```
Hypothesis RuleBasedStateMachine (sync)
        │  each rule → loop.run_until_complete(coro); loop+conn owned per instance
        ▼
  System Under Test (async):
    in-memory aiosqlite db (conftest SCHEMA)
      ├─ TradeIntentStore      (real)
      ├─ TrimLadderStore       (real)
      ├─ PositionExitStore     (real)
      ├─ SellFollower.run()    (real; is_rth=lambda: True)
      ├─ OrderSizer.run()      (real; stateless INV-4 path)
      └─ FakeGateway           (deterministic, configurable fills)   ← only fake
        ▲
  Shadow model (plain Python):
    per-intent fill_qty; armed/claimed/recorded rungs;
    claimed sell fingerprints + per-fingerprint invocation/positive-sell counts
```

Reuses existing patterns: the schema from `infra/storage/db.py`, the filled-intent
shape from `test_sell_follower.py::_filled_intent`, and the `SellFollower(...)`
construction used there.

## 6. The State Machine — Operation Alphabet

Rules form the alphabet of things that can happen. The **key technique** is
**splitting "claim/begin" from "commit/complete"**, so two sequential rules
deterministically reproduce real concurrency windows — no threads.

- **`create_filled_intent(channel, ticker, fill_qty)`** — insert a filled equity
  intent (`TradeIntentStore.insert`). Shadow: register fill_qty.
- **`arm_trims(intent, rungs)`** — `TrimLadderStore.arm`. Shadow: mark armed rungs.
- **`begin_trim_fire(intent, rung)`** — `claim_for_fire` only (in-flight, not
  recorded). First call on a rung returns `True`; a second `begin` returns `False`
  (**INV-3**). Shadow: rung in-flight; reserve = `round_half_up_min1(fill_qty·trim_pct)`.
- **`complete_trim_fire(intent, rung, fill_fraction)`** — `record_fire` with
  `sold_qty = floor(reserve · fill_fraction)` (models partial/zero/full). Shadow:
  add to recorded sells; rung recorded.
- **`release_trim(intent, rung)`** — `release_claim` (broker-unavailable path);
  only valid while in-flight & unrecorded. Shadow: clear reserve.
- **`follow_sell(channel, ticker, scope, fraction, fingerprint, fill_mode)`** —
  configure `FakeGateway` (`full`/`partial`/`zero`/`unavailable`) and run the
  **real** `SellFollower.run(ctx)` to completion. `fingerprint` may be reused to
  model a repost. Shadow: track per-fingerprint invocation + positive-sell counts
  (**INV-2**) and placed-order count.
- **`begin_sell` / `complete_sell` (§3a probe)** — split variant that places the
  sell, *pauses before recording the exit*, lets other rules interleave (a trim
  fire, a second-fingerprint sell), then records. This is the targeted probe for
  the in-flight-sell oversell. Implemented by faking the gateway so `wait_fill`
  yields control / by recording in a second rule.
- **`crash_restart()`** — models process death between claim and record. Persisted
  state is untouched (a claimed-but-unrecorded rung stays reserved — `all_unfired`
  excludes it, so it is never auto-re-fired); in-memory loop state is forgotten.
  Asserts subsequent sells still cannot oversell and no rung auto-double-fires.

After each rule, the **invariant oracle** (§3) runs against the live DB plus the
shadow model.

A separate, **stateless** `@given` property covers **INV-4** (`OrderSizer`),
gated on `"quantity" in updates` per §3.

## 7. FakeGateway

A deterministic class in `tests/support/fake_gateway.py` (generalizing the
`MagicMock`-based `_gw` in `test_sell_follower.py`):

- `qualify_equity`, `get_quote` → fixed deterministic values.
- `get_account_summary` → returns a real `AccountSummary(net_liquidation,
  buying_power, currency)` (configurable) for the `OrderSizer` path.
- `place_order` / `wait_fill` → a `FillResult` per a configurable `fill_mode`
  (full / partial / zero); records every placed order for assertions; supports the
  §3a "pause before record" probe.
- `unavailable` toggle → raises `IBGatewayUnavailable` from `place_order`.
- `cancel_order` → records the cancel (asserts residual-cancel discipline).

## 8. Determinism & the Async/Hypothesis Recipe (spike-confirmed)

- Inject `is_rth=lambda: True`; pass explicit `started_at`/timestamps to claims;
  fixed gateway prices; **no wall-clock or RNG in test logic** (Hypothesis owns
  randomness). Audit confirmed no store branches on wall-clock — only `is_rth`,
  which is injectable.
- **Confirmed recipe (spike green):**
  - Own ONE event loop + ONE connection **per machine instance**, created in
    `__init__` (after `super().__init__()`) via `asyncio.new_event_loop()` +
    `run_until_complete(setup)`. Hypothesis builds a fresh instance per example →
    each example gets a pristine in-memory DB (aiosqlite `:memory:` is
    per-connection). Do **not** use a pytest fixture for the DB (fixtures are
    per-function, not per-example); do **not** call `set_event_loop()`.
  - Each `@rule`/`@invariant` is sync and calls `self._loop.run_until_complete(...)`;
    batch awaits per call.
  - `teardown()` (runs on pass and fail): **close the connection first, then the
    loop** — order matters (avoids aiosqlite worker-thread / dead-loop
    ResourceWarnings).
  - `@settings(deadline=None, derandomize=True, database=None,
    suppress_health_check=[HealthCheck.too_slow], max_examples=…,
    stateful_step_count=…)`; expose via `TestX = Machine.TestCase`. `deadline=None`
    is mandatory (async-over-thread steps aren't instantaneous).
  - **CRITICAL placement gotcha:** the test files MUST live under the project
    `tests/` tree so `pyproject.toml`'s `asyncio_mode = "auto"` applies (pytest
    resolves rootdir from file location). Under auto mode the sync `TestCase` is
    left alone — harmless.

## 9. File Layout, Dependencies, Runtime

- `tests/support/__init__.py`, `tests/support/fake_gateway.py` — shared fake.
- `tests/property/__init__.py`, `tests/property/test_position_invariants.py` — the
  state machine + §3 oracle (under `tests/` so asyncio auto-mode applies — §8).
- `tests/property/test_sizing_properties.py` — stateless `@given` for INV-4.
- `pyproject.toml`: add `hypothesis>=6` to `[project.optional-dependencies].dev`
  (installed: `6.155.7`); register a `ci` Hypothesis profile (more examples) vs. a
  fast `dev` default.
- Runs under plain `pytest`; stays deterministic/offline. Target: the property
  module completes in **< ~30s** under the default profile.

## 10. Findings Policy

If a property fails, it is surfaced as a **finding** with its minimal
counterexample — a real bug *or* an over-strict property, decided per-case. **We
never silently weaken a property, and never edit production code to make a property
pass without explicit sign-off.** The audit's **INV-1 in-flight-sell oversell
(§3a)** is the first candidate finding the suite targets; if the probe confirms it,
the proposed fix (reserve in-flight sells in `remaining_qty`) is raised separately.

## 11. Success Criteria

- The state machine runs green over many randomized sequences (default + `ci`
  profiles) for the run-to-completion invariants; the §3a probe either stays green
  (refuting the finding) or yields a minimal reproducible counterexample
  (confirming it). Both outcomes are wins.
- New tests are deterministic and offline; full `pytest` stays green (excluding any
  confirmed §3a finding, which is tracked as a bug); added runtime within §9.

## 12. Future Extensions (out of scope here)

- Options-sleeve invariants (parent/child intent legs).
- LLM confidence-calibration + live-LLM drift eval (separate spec & cadence).
- Historical-price economic backtest layered on the replay harness.
