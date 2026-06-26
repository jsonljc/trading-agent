# Money-Safety Invariant Suite — Design

- **Date:** 2026-06-26
- **Branch:** fix/north-star-hardening
- **Status:** Approved (design) — pending spec review, then implementation plan
- **Author:** Jason + Claude

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
  reproducible counterexample* (a real finding).

**Non-Goals (deferred to separate specs)**
- Options-sleeve invariants (shares path only for v1).
- LLM confidence-calibration and live-LLM drift evals (different subsystem,
  different cadence, costs API calls).
- Historical-price economic backtest.
- True multi-threaded concurrency — modeled here via deterministic
  *interleaving* of sequential rules, not OS threads.

## 3. The Invariants (the oracle)

Checked after **every** operation. Each references the production code it guards.

- **INV-1 — Never oversell (per intent).** For every intent,
  `Σ(recorded trim sold_qty) + Σ(recorded exit sold_qty) ≤ fill_qty`.
  Guards: `SellFollower.run` oldest-first allocation + `follow_sell_position`;
  trim `record_fire`.
- **INV-2 — Claim-once idempotency.** A sell message fingerprint produces exits
  from at most one `SellFollower` invocation, unless explicitly released after a
  *true zero-sale* (`release_sell_event`). A redelivered/edited repost with the
  same fingerprint never sells again.
  Guards: `PositionExitStore.claim_sell_event` / `release_sell_event`.
- **INV-3 — No trim double-fire.** `TrimLadderStore.claim_for_fire(intent, rung)`
  succeeds at most once until released; a rung records a fire at most once.
  Guards: `claim_for_fire` (`WHERE fired_at IS NULL AND fire_started_at IS NULL`).
- **INV-4 — Sizing caps (stateless).** `OrderSizer` never emits a `success` whose
  `quantity·unit_cost` exceeds live `buying_power`, nor one that pushes
  `open_exposure + notional` past `aggregate_notional_cap`; never emits
  `quantity < 1` as success (it `skip`s instead).
  Guards: `OrderSizer.run` buying-power clamp + aggregate-exposure check.
- **CONSISTENCY — Remaining-qty identity.** `PositionExitStore.remaining_qty(intent)`
  always equals `fill_qty − recordedTrims − recordedExits − inFlightTrimReserves`,
  and is `≥ 0`. This is what protects a sell that races an in-flight trim.

**Design choice — assert invariants, not a reimplementation.** The oracle checks
*inequalities and idempotency* (properties that must hold regardless of internal
allocation order), **not** exact predicted quantities. We never re-implement
`SellFollower`'s allocation in the test; that would just test the test.

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
        │  each rule → run_until_complete(coro) on a shared event loop
        ▼
  System Under Test (async):
    in-memory aiosqlite db (conftest SCHEMA)
      ├─ TradeIntentStore      (real)
      ├─ TrimLadderStore       (real)
      ├─ PositionExitStore     (real)
      ├─ SellFollower.run()    (real; is_rth=lambda: True)
      └─ FakeGateway           (deterministic, configurable fills)   ← only fake
        ▲
  Shadow model (plain Python):
    per-intent fill_qty; set of armed/claimed/recorded rungs;
    set of claimed sell fingerprints + their invocation count
```

Reuses the existing fixtures/patterns: the `db` fixture (`tests/conftest.py`),
the filled-intent shape from `test_sell_follower.py::_filled_intent`, and the
`SellFollower(...)` construction already used there.

## 6. The State Machine — Operation Alphabet

Rules form the alphabet of things that can happen. The **key technique** is
**splitting "claim" from "commit"**, so two sequential rules deterministically
reproduce the real "2-second poll vs. >2s broker round-trip" race — no threads.

- **`create_filled_intent(channel, ticker, fill_qty)`** — insert a filled equity
  intent (`TradeIntentStore.insert`). Shadow: register fill_qty.
- **`arm_trims(intent, rungs)`** — `TrimLadderStore.arm`. Shadow: mark armed rungs.
- **`begin_trim_fire(intent, rung)`** — `claim_for_fire` only (in-flight, not
  recorded). First call on a rung must return `True`; a second `begin` on the same
  rung must return `False` (**INV-3**). Shadow: rung in-flight, reserve =
  `round_half_up_min1(fill_qty·trim_pct)`.
- **`complete_trim_fire(intent, rung, fill_fraction)`** — `record_fire` with
  `sold_qty = floor(reserve · fill_fraction)` (`0 ≤ fraction ≤ 1`, models
  partial/zero/full fills). Shadow: add to recorded sells; clear reserve.
- **`release_trim(intent, rung)`** — `release_claim` (broker-unavailable path);
  only valid while in-flight & unrecorded. Shadow: clear reserve.
- **`follow_sell(channel, ticker, scope, fraction, fingerprint, fill_mode)`** —
  configure `FakeGateway` (`full` / `partial` / `zero` / `unavailable`) and run the
  **real** `SellFollower.run(ctx)`. `fingerprint` may be reused to model a
  repost. Shadow: record that this fingerprint was attempted; track place_order
  count to detect a second sale on the same fingerprint (**INV-2**).
- **`crash_restart()`** — models process death between claim and record. Persisted
  state is untouched (a claimed-but-unrecorded rung stays reserved — `all_unfired`
  excludes it, so it is never auto-re-fired). In-memory loop state is forgotten.
  Asserts: subsequent sells still cannot oversell (the reserve protects them) and
  no rung auto-double-fires after restart.

After each rule, the **invariant oracle** (§3) runs against the live DB plus the
shadow model.

A separate, **stateless** `@given` property covers **INV-4** (`OrderSizer`), since
sizing is a function of `(net_liq, buying_power, size_pct, price, open_exposure,
aggregate_cap)` and needs no state machine.

## 7. FakeGateway

A small deterministic class in `tests/support/fake_gateway.py` (lifting and
generalizing the `MagicMock`-based `_gw` in `test_sell_follower.py`):

- `qualify_equity`, `get_quote` → fixed deterministic values.
- `place_order` / `wait_fill` → return a `FillResult` per a configurable
  `fill_mode` (full / partial / zero); records every placed order for assertions.
- `unavailable` toggle → raises `IBGatewayUnavailable` from `place_order`.
- `cancel_order` → records the cancel (asserts residual-cancel discipline).

Shared so both the stateful machine and any future property tests use one fake.

## 8. Determinism & the Async/Hypothesis Wrinkle

- Inject `is_rth=lambda: True`; pass explicit `started_at`/timestamps to claims;
  fixed gateway prices; **no wall-clock or RNG in test logic** (Hypothesis owns
  randomness via its generators).
- **Main implementation risk:** Hypothesis `RuleBasedStateMachine` rules are
  **sync**, but the stores are **async** (aiosqlite). Resolution: hold one shared
  event loop + one connection for the machine instance and
  `loop.run_until_complete(...)` each rule's coroutine. The DB and stores are
  created in the machine's `__init__`/first rule and torn down at teardown. This
  is the one part to prototype first.

## 9. File Layout, Dependencies, Runtime

- `tests/support/__init__.py`, `tests/support/fake_gateway.py` — shared fake.
- `tests/property/test_position_invariants.py` — the state machine + §3 oracle.
- `tests/property/test_sizing_properties.py` — stateless `@given` for INV-4.
- `pyproject.toml`: add `hypothesis>=6` to `[project.optional-dependencies].dev`;
  register a `ci` Hypothesis profile (more examples) vs. a fast `dev` default.
- Runs under plain `pytest`; stays deterministic/offline. Target: the property
  module completes in **< ~30s** under the default profile so it can live in the
  normal suite.

## 10. Findings Policy

If a property fails, it is surfaced as a **finding** with its minimal
counterexample — it may be a real bug *or* an over-strict property. We decide
per-case. **We never silently weaken a property, and never edit production code
to make a property pass without explicit sign-off.**

## 11. Success Criteria

- The state machine runs green over many randomized sequences (default + `ci`
  profiles), giving pre-go-live evidence the invariants hold; **or** it yields a
  minimal reproducible counterexample (a real finding). Both outcomes are wins.
- New tests are deterministic and offline; full `pytest` stays green; total
  added runtime within the budget in §9.

## 12. Future Extensions (out of scope here)

- Options-sleeve invariants (parent/child intent legs).
- LLM confidence-calibration + live-LLM drift eval (separate spec & cadence).
- Historical-price economic backtest layered on the replay harness.
