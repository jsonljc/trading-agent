# Phase E — Sell-Following: Design & Plan (rev. 2, post design-review)

**Goal:** Follow the tracked trader's **explicit sells**. Today exit/sell messages
classify as SKIP and are dropped — entries fire but exits never do. This is the
sanctioned downside (hold until the trader sells, then follow) and completes the
trade lifecycle.

**Scope (v1): shares-only.** Detect an explicit sell → match our open *shares*
position(s) for that (channel, ticker) → submit a marketable-limit SELL for the held
qty (full or partial) → record the exit idempotently. The **options leg is left
held** (consistent with the trim ladder, which is shares-only) — a documented
follow-up, because selling-to-close an option needs an option *bid* source the
gateway doesn't expose (`get_option_ask` only) plus contract reconstruction.

**Branch:** `feat/phase-e-sell-following` (off the C+D HEAD). TDD, one commit/task.
Paper mode only. NO automated stop-loss / kill-on-P&L — selling is driven **solely**
by the trader's explicit sell message.

> This rev incorporates an adversarial design review that caught 3 blockers
> (event_id-keyed idempotency double-selling reposts; per-intent partial fraction
> over-selling; a permanent-claim vs. releasable-claim contradiction) and several
> important issues (audit mislabel, in-flight trim race, RTH, mixed messages).

---

## Architecture — two self-gating skills (no orchestrator refactor)

The pipeline is ONE flat chain (`main.py`: `full_chain = phase1 + phase2b`, run by a
single Orchestrator that halts on skip/fail). Add two skills that fit the existing
self-gating pattern (the options sub-chain already no-ops via `already_terminated`).

**Pinned chain order** (within `build_phase1_chain`):
`MessageNormalizer → TraderRouter → TraderClassifier → SellClassifier →
ClassificationLogger → SameDayDedupGate → SellFollower → EntrySkipGate →
IdempotencyCheck → TickerValidator → TelegramDigest` → (phase2b entry chain).

- **`SellClassifier`** (after `TraderClassifier`, before `ClassificationLogger` so
  the sell is logged): runs only when an **exit verb** is present AND the entry
  classifier produced no actionable entry (`bucket` not in HIGH/LOW). LLM
  (fail-closed) → `{is_sell, ticker, scope: full|partial, fraction, confidence}`. On
  a confident sell sets `ctx.action="sell"` + `sell_scope`/`sell_fraction`.
  Otherwise no-op (entries untouched). **Mixed-message policy (explicit):** if the
  entry classifier already produced HIGH/LOW, the entry wins and the exit is
  dropped — documented v1 behavior.
- **`SellFollower`** (after `SameDayDedupGate`, before `EntrySkipGate`):
  - `action != "sell"` → `success` (pass-through; entries proceed).
  - `action == "sell"` → execute, then return `skip` with a reason. `on_skip`
    branches on the reason to audit + alert correctly (see Audit semantics).

`EntrySkipGate` is unchanged. For an entry, `SellFollower` passes through; for a
sell, the chain already halted at `SellFollower` so execution never runs.

## Sell detection (SellClassifier)

- **Feature extractor:** add `_EXIT_VERBS` (sold, sold out, out of, closed, close,
  trimmed/trimming, taking/took profits, scaling out, exiting, dumped, cut, stopped
  out) → `Features.exit_verb_present: bool`. Entry-verb extraction unaffected.
- **LLM contract:** `{is_sell, ticker, scope, fraction, confidence, reason}`. New
  optional per-trader `sell_examples` profile field (defaults empty).
- **Fail-closed gates:** require `exit_verb_present`, `is_sell`, ticker present in
  the message (anti-hallucination), `confidence >= 0.70`. `partial` with no
  parseable fraction → default 0.5.

## Idempotency — fingerprint-keyed, RTH-gated, no release

The review showed event_id-keyed claims double-sell reposts (new event_id, same
content) and that "permanent UNIQUE claim" can't be "released for retry". Resolution:

- **Key on the message fingerprint** (`ctx.message_fingerprint`,
  content-based, stable across reposts/edits) — NOT event_id.
- **RTH gate first:** outside Regular Trading Hours → `skip` (`sell_outside_rth`) +
  operator alert, **without claiming**. RTH marketable-limit sells fill reliably, so
  we never need the releasable-claim/retry machinery; a repost during RTH is then
  free to proceed.
- **`claim_sell_event(fingerprint, event_id)`**: `INSERT OR IGNORE` into
  `sell_event_claims(fingerprint PK, event_id, claimed_at)`, returns `rowcount>0`.
  Permanent, atomic, dedups concurrent/redelivered/reposted sells. A rare RTH
  zero-fill is **not** auto-retried — it's alerted for manual handling (matches the
  deliberate no-auto-anything-risky philosophy).

## Position matching, aggregate fraction, remaining qty

- **`get_open_shares_positions(channel, ticker)`**: `execution_state='filled'`,
  `instrument_type='equity'` (verified against `TradeIntentWriter`'s stored literal
  by a test), this (channel, ticker), oldest-first. Channel-scoped so a trader's
  sell only closes that trader's position.
- **`remaining_qty(intent_id)`** = `fill_qty − Σ(trim qty) − Σ(position_exits
  sold_qty)`, where trim qty counts **recorded** rungs' `sold_qty` AND **reserves**
  in-flight claimed-but-unrecorded rungs (`fire_started_at` set, `fired_at` NULL) at
  `round_half_up_min1(fill_qty × trim_pct)`. Reserving in-flight trims closes the
  trim/sell race the review flagged (worst case otherwise: one rung double-sold).
- **Aggregate fraction (fixes per-intent over-sell):** compute `agg_remaining = Σ
  remaining_qty` across matched intents. Target = `agg_remaining` (full) or
  `floor(agg_remaining × fraction)` min 1 (partial). Allocate the target across
  intents oldest-first (`min(remaining_i, target_left)`), one SELL order per intent.

## position_exits ledger

```sql
CREATE TABLE position_exits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint      TEXT NOT NULL,    -- sell-event content hash (idempotency)
  event_id         TEXT,             -- triggering message (trace)
  intent_id        TEXT NOT NULL,    -- shares intent sold
  channel          TEXT, ticker TEXT,
  scope            TEXT,             -- 'full' | 'partial'
  requested_qty    INTEGER,
  sold_qty         INTEGER,
  sold_avg_price   REAL,
  broker_order_ref TEXT,
  reason           TEXT,
  created_at       TEXT NOT NULL
);
```
Plus `sell_event_claims(fingerprint TEXT PRIMARY KEY, event_id TEXT, claimed_at TEXT
NOT NULL)`. Both go in `SCHEMA` **and** idempotent `_migrate()` `CREATE TABLE IF NOT
EXISTS` (db.py convention).

## Sell execution (module helper `follow_sell_position`, mirrors fire_rung_if_crossed)

Per allocated intent: `qualify_equity(ticker)`; `price = get_quote(ticker)`;
`limit = marketable_sell_limit(price, cap)`; `PreparedOrder(action="SELL", qty,
"LMT", limit, "DAY")`; `client_order_id = f"{intent_id}:exit:{fingerprint[:8]}"`;
`place_order` → `wait_fill`. Reuses the C+D partial-fill discipline: broker-down →
fail; `filled_qty==0` → cancel residual; partial → cancel residual + record real
`sold_qty`; record one `position_exits` row with the actual sold qty. Inline
`wait_fill` blocks the handler up to `fill_timeout` — same as the existing entry
submitters (the deferred concurrency item covers both).

## Audit semantics + wiring (fixes the "skipped" mislabel)

`SellFollower` returns `skip` with a precise reason. `main.py on_skip` branches:
- `sell_followed` → `audit_writer.write(ctx, "sell_followed")` (NOT "skipped", so P&L
  analytics aren't corrupted) + `digest.send_sell_digest(ctx)`.
- `no_open_position` / `sell_outside_rth` / `sell_already_followed` →
  `audit_writer.write(ctx, "skipped")` + a one-line informational alert.
- `TelegramDigest.send_sell_digest(ctx)`: "✅ FOLLOWED SELL — {ticker} sold {qty} @
  {px} ({scope})".

Registry/main wiring: construct the exits + claim store, thread gateway / intent
store / llm / trader registry / `shares_slippage_cap_pct` into the two skills.

## Test surface (TDD)
- feature extractor: exit verbs detected; entry verbs unaffected; mixed → both flags.
- SellClassifier: exit verb + is_sell → action='sell' + scope/fraction; no exit verb
  → no-op; low conf / ticker-not-in-msg → no-op; entry (HIGH/LOW) message → untouched
  (mixed-message: entry wins).
- store: `get_open_shares_positions` (channel+ticker+filled+equity literal),
  `remaining_qty` (nets recorded trims + exits AND reserves in-flight trims),
  `claim_sell_event` idempotency (repost same fingerprint → second claim False),
  `record_exit`.
- `follow_sell_position`: full = remaining; partial = floor(agg×fraction) allocated
  oldest-first; partial-fill cancels residual + records real qty; zero-fill cancels
  residual; double-claim no-op; remaining 0 → no-op.
- SellFollower: action='sell' RTH → executes + skip('sell_followed'); outside RTH →
  skip('sell_outside_rth') no claim; entry → pass-through; no position →
  skip('no_open_position'); reposted fingerprint → skip('sell_already_followed').
- migration: both new tables created on a legacy DB.
- e2e: filled shares intent → sell message (RTH) → position sold, exit recorded,
  entry chain not run, audit status='sell_followed'.

## Deferred (documented)
- Options-leg sell-to-close (needs `get_option_bid` + contract reconstruct).
- RTH zero-fill auto-retry (alert-only in v1); reworded-partial edge cases.
- Other Phase E items (P&L attribution, replay, classifier eval, active-learning,
  economic guards) — separate specs.
