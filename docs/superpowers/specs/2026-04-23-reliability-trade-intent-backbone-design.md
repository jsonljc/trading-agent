# Spec 1 — Reliability & TradeIntent Backbone

**Date:** 2026-04-23
**Goal:** Give every signal a canonical, durable record from post-parse intent creation to fill or terminal state.
Separate policy decisions from execution failures. Support fast trading without ambiguity about
whether an order was placed, filled, or lost.

---

## Problem

The current system carries all trade state in a loose `Context` dict that is never persisted as a
typed record. This means:

- No single table answers "what signals came in, what was decided, what was executed, and why."
- Crashes between broker call and DB write leave mystery state.
- Policy denials (channel blocked, cooldown) look the same as runtime failures.
- There is no latency record, so "where is the slowness?" is unanswerable.

---

## Architecture

A `trade_intents` row is created immediately after `SignalAnalyzer` output is parsed and before
deterministic validation, channel policy gating, cooldown checks, or any execution begins. This
makes the intent row the canonical record for both policy denials and execution outcomes. Every
downstream feature anchors to this single row.

Two independent state tracks:

```
Policy track:
  approved          → execution runs
  channel_blocked   → terminal, Telegram log only, no DLQ
  cooldown_blocked  → terminal, Telegram log only, no DLQ
  ambiguous_signal  → terminal, Telegram log only, no DLQ (set by TickerValidator in Spec 2)

Execution track:
  pending → submitted → filled              ✓ success
                      → cancelled_unfilled   walk exhausted or cap hit — Telegram alert, no DLQ
                      → failed              broker / API / system error → DLQ + Telegram
```

---

## Schema — trade_intents

```sql
CREATE TABLE trade_intents (
  -- Identity (contract-specific)
  -- Options:  {event_id}:{ticker}:{side}:{expiry}:{strike}:{right}
  -- Equity:   {event_id}:{ticker}:{side}:equity
  intent_id              TEXT PRIMARY KEY,
  event_id               TEXT NOT NULL,
  channel                TEXT NOT NULL,
  ticker                 TEXT NOT NULL,
  side                   TEXT NOT NULL,    -- long | short
  instrument_type        TEXT NOT NULL,    -- option | equity
  expiry                 TEXT,             -- null for equity
  strike                 REAL,             -- null for equity
  right                  TEXT,             -- C | P | null for equity
  conviction             TEXT NOT NULL,    -- high | medium | low

  -- Signal analysis metadata (from SignalAnalyzer)
  analysis_confidence    REAL,
  ambiguity_flags        TEXT,             -- JSON array
  rationale              TEXT,
  ticker_raw             TEXT,             -- LLM raw output before validation
  side_raw               TEXT,
  conviction_raw         TEXT,

  -- Reference spot (recorded at chain lookup time)
  reference_spot_price   REAL,
  reference_spot_timestamp TEXT,

  -- Policy outcome (decided at Phase 2b entry, terminal)
  policy_state           TEXT NOT NULL,    -- approved | channel_blocked | cooldown_blocked
                                           --   | ambiguous_signal

  -- Execution strategy (encoded at order time)
  -- policy_state is the source of truth for whether execution is permitted.
  -- execution_mode is only meaningful for intents that remain executable after
  -- validation and policy gating; null for terminal policy-blocked intents.
  execution_mode         TEXT,             -- auto_live | observe | null
  order_type             TEXT,             -- marketable_limit
  walk_profile           TEXT,             -- cautious_fast | aggressive_fast
  initial_reference_ask  REAL,             -- ask when first order placed
  initial_order_limit    REAL,             -- actual first submitted limit (may differ)
  max_chase_pct          REAL,             -- e.g. 0.15 = 15% above initial_reference_ask
  max_chase_price        REAL,             -- computed: initial_reference_ask × (1 + max_chase_pct)
  max_reprices           INTEGER,          -- 3
  reprice_interval_ms    INTEGER,          -- 2500

  -- Execution outcome
  execution_state        TEXT,             -- pending | submitted | filled |
                                           --   cancelled_unfilled | failed
  outbox_status          TEXT,             -- pending | dispatched | confirmed
  broker_order_ref       TEXT,
  order_attempt_count    INTEGER,          -- starts at 1, increments per cancel+replace
  last_limit_price       REAL,             -- final price attempted in walk
  fill_price             REAL,
  dlq_reason             TEXT,             -- only populated on failed state

  -- Cancel reason (enumerated)
  -- stale_signal = pre-order invalidation (signal aged out before execution began)
  -- stale_quote  = quote-age failure inside PriceWalker mid-walk
  cancel_reason          TEXT,             -- walk_exhausted | price_exceeded_cap |
                                           --   manual_cancel | stale_signal |
                                           --   market_closed | fill_timeout | stale_quote

  -- Latency timestamps
  signal_received_at     TEXT NOT NULL,
  intent_created_at      TEXT NOT NULL,
  order_submitted_at     TEXT,
  order_ack_at           TEXT,
  filled_at              TEXT,
  cancelled_at           TEXT,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);
```

Per-attempt submission and acknowledgment timestamps are intentionally not modeled in v1. The
schema captures first-submit and terminal timings only; attempt-level execution history can be
added later if fine-grained walk diagnostics become necessary.

Derived latency metrics are computed at query time from timestamps:
- `signal_received_at → intent_created_at` = orchestration overhead
- `intent_created_at → order_submitted_at` = chain lookup + sizing time
- `order_submitted_at → order_ack_at` = IBKR placement RTT
- `order_ack_at → filled_at` = market fill time
- `signal_received_at → filled_at` = total pipeline latency

---

## Per-Channel Auto-Execute Policy

`policy.yaml` gains a per-channel `auto_execute` flag:

```yaml
watched_channels:
  mystic:
    auto_execute: true
  pup-danny:
    auto_execute: true
  chat:
    auto_execute: false    # observe only
  wall-st-engine:
    auto_execute: false
  alerts:
    auto_execute: true
```

A `ChannelPolicyGuard` skill at Phase 2b entry checks this. If `auto_execute: false`, writes
`policy_state: channel_blocked` and returns `skip` — no execution, no DLQ.

---

## Transactional Outbox

Before `PriceWalker` makes the first broker-side effect, the intent row is updated to
`outbox_status: pending`. After IBKR acknowledges the first live order, `outbox_status` is
updated to `dispatched`. After terminal execution success (filled), `outbox_status` is updated
to `confirmed`. This field is used for crash recovery and reconciliation of broker-side effects,
not as a general business workflow state.

On restart, `ExecutionReconciler` scans for rows stuck in `pending` or `dispatched` and
reconciles against IBKR open orders. Prevents "did we place it or not?" ambiguity after
crashes.

---

## Dead-Letter Queue

Any Phase 2b skill that returns `status: fail` and cannot recover writes:
- `execution_state: failed`
- `dlq_reason: <reason string>`
- Triggers Telegram alert

A `dlq_intents` view surfaces these for inspection:
```sql
CREATE VIEW dlq_intents AS
  SELECT * FROM trade_intents
  WHERE execution_state = 'failed'
  ORDER BY created_at DESC;
```

Policy-blocked intents (`channel_blocked`, `cooldown_blocked`, `ambiguous_signal`) are
**not** in this view. They are separate terminal states, not operational failures.

---

## Cooldown Policy

Re-enabled in config. `CooldownGuard` skill queries `trade_intents` for any filled trade on
the same ticker within the cooldown window before executing. If within window, writes
`policy_state: cooldown_blocked` — terminal, no execution.

```yaml
cooldown_policy:
  enabled: true
  cooldown_minutes: 30    # per-ticker cooldown after a fill
```

Cooldown is a risk control, not a speed feature. It may block some trades by design.
It is configurable per channel if needed (future).

---

## New Skills / Changes

| Component | Change |
|---|---|
| `ChannelPolicyGuard` | New skill, Phase 2b entry |
| `CooldownGuard` | New skill, Phase 2b entry (after ChannelPolicyGuard) |
| `TradeIntentWriter` | New skill, writes intent row at Phase 2b entry |
| `ExecutionAuditWriter` | Update to write outbox status columns |
| `ExecutionReconciler` | Update to scan outbox_status: pending / dispatched |
| `policy.yaml` | Add per-channel auto_execute flags |
| `db.py` | Add trade_intents table migration |

---

## What This Does Not Change

- Phase 1 pipeline (signal parsing, idempotency) is unchanged
- `IBGateway` interface is unchanged
- Execution skill chain order is unchanged (new guard skills prepended to Phase 2b)
