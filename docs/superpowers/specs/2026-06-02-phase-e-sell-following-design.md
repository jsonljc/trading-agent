# Phase E — Sell-Following: Design & Plan

**Goal:** Follow the tracked trader's **explicit sells**. Today exit/sell messages
classify as SKIP and are dropped — so entries fire but exits never do. This is the
sanctioned downside (the deliberate copy-trade thesis: hold until the trader sells,
then follow), and it completes the trade lifecycle.

**Scope (v1): shares-only.** Detect an explicit sell → match our open *shares*
position(s) for that (channel, ticker) → submit a marketable-limit SELL for the held
qty (full or partial) → record the exit idempotently. The **options leg is left
held** in v1 (consistent with the existing trim ladder, which is shares-only) — a
documented follow-up, because selling-to-close an option needs an option *bid*
source the gateway doesn't expose yet (`get_option_ask` only) and contract
reconstruction from the stored intent.

**Branch:** `feat/phase-e-sell-following` (off the C+D HEAD). TDD, one commit/task.
Paper mode only. NO automated stop-loss / kill-on-P&L is added — selling is driven
**solely** by the trader's explicit sell message.

---

## Architecture — two new skills, self-gating (no orchestrator refactor)

The pipeline runs one flat chain per event and halts on skip/fail; `EntrySkipGate`
halts every SKIP. Rather than refactor into branched orchestrators, add two skills
that fit the existing self-gating pattern (mirroring how the options sub-chain
no-ops via `already_terminated`):

1. **`SellClassifier`** (phase1, after `TraderClassifier`): only runs when an **exit
   verb** is present (cheap deterministic prefilter) AND the entry classifier
   produced no actionable entry. LLM (fail-closed) detects the sell and extracts
   `{ticker, scope: full|partial, fraction, confidence}`. On a confident sell it
   sets `ctx.action="sell"` + `sell_*` keys. Otherwise no-op (entries untouched).

2. **`SellFollower`** (placed right after `SellClassifier`, BEFORE `EntrySkipGate`):
   - `action != "sell"` → `success` (pass-through; entries proceed normally).
   - `action == "sell"` → execute the sell, then return `skip` (reason
     `sell_followed`) to halt the entry path (a sell is not an entry). The actual
     sell is recorded in `position_exits` + the trade audit; `on_skip` sends a sell
     digest.

`EntrySkipGate` is unchanged — for a real entry, `SellFollower` passes through and
the gate behaves as today; for a sell, the chain already halted at `SellFollower`.

## Sell detection (SellClassifier)

- **Feature extractor:** add `_EXIT_VERBS` (sold, sold out, out of, closed, close,
  trimmed/trimming, taking profits, scaling out, exiting, dumped, cut, took profits,
  stopped out) → `Features.exit_verb_present: bool`.
- **LLM contract:** `{is_sell: bool, ticker: SYMBOL|null, scope: "full"|"partial",
  fraction: 0.0-1.0|null, confidence: 0.0-1.0, reason: str}`. Per-trader optional
  `sell_examples` on the profile (new field, defaults empty) teach it.
- **Fail-closed gates:** require `exit_verb_present`, `is_sell`, ticker present in
  message (anti-hallucination, same as entries), `confidence >= 0.70`. Partial with
  no parseable fraction → default 0.5.

## Position matching + remaining quantity

- **`get_open_shares_positions(channel, ticker)`** on `TradeIntentStore`: filled,
  equity, `execution_state='filled'`, this (channel, ticker). (Channel-scoped so a
  trader's sell only closes that trader's position.)
- **Remaining qty** = `fill_qty − Σ(trim_ladder sold_qty) − Σ(position_exits
  sold_qty)`. Helper `remaining_qty(intent_id)` joins the two sell sources. A
  position with remaining ≤ 0 is already closed → skipped (this is also what makes a
  reworded re-post of a *full* exit a no-op).

## position_exits table + idempotency

```sql
CREATE TABLE position_exits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id        TEXT NOT NULL,     -- shares intent being sold
  exit_event_id    TEXT NOT NULL,     -- the sell message event_id
  scope            TEXT NOT NULL,     -- 'full' | 'partial'
  requested_qty    INTEGER,
  claimed_at       TEXT,
  sold_at          TEXT,
  sold_qty         INTEGER,
  sold_avg_price   REAL,
  broker_order_ref TEXT,
  reason           TEXT,
  UNIQUE(intent_id, exit_event_id)
);
```
(New table in SCHEMA **and** an idempotent `_migrate()` `CREATE TABLE IF NOT EXISTS`,
per the db.py convention.) **Idempotency:** `claim_exit(intent_id, exit_event_id)` =
`INSERT OR IGNORE` returning `rowcount>0` — the same sell message can't double-fire
on one position (concurrent or re-delivered). The remaining-qty guard handles
reworded full-exit reposts (remaining hits 0). Mirrors the trim ladder's
claim-before-order discipline.

## Sell execution (module helper, mirrors fire_rung_if_crossed)

`follow_sell_position(gw, exits_store, intent_id, ticker, qty, exit_event_id, scope,
cap)`:
1. `claim_exit(...)` → bail if already claimed.
2. `qualify_equity(ticker)`; `price = get_quote(ticker)`;
   `limit = marketable_sell_limit(price, cap)`.
3. `PreparedOrder(action="SELL", qty, "LMT", limit, "DAY")`;
   `client_order_id = f"{intent_id}:exit:{exit_event_id}"`.
4. `place_order` → `wait_fill`. Broker-unavailable → release claim, ret/ fail.
   filled_qty==0 → cancel residual + release claim (retry-able) → fail.
   partial → cancel residual, record real sold_qty. filled → record.
5. `record_exit(...)` with sold_qty/avg/broker_ref. Reuses the C+D partial-fill
   discipline (cancel residual, never mask a fill).

For **full** scope, qty = remaining; for **partial**, qty = `floor(remaining ×
fraction)` (min 1 if remaining ≥ 1). When multiple open intents match a (channel,
ticker), apply to each oldest-first.

## Wiring

- `agent/registry.py`: add `SellClassifier` + `SellFollower` to `build_phase1_chain`
  (after `TraderClassifier`, before `EntrySkipGate`). Both need the gateway, intent
  store, exits store, llm, trader registry, slippage cap.
- `main.py`: construct the exits store; pass deps; `on_skip` sends a **sell digest**
  when reason == `sell_followed`.
- `TelegramDigest.send_sell_digest(ctx)`: "FOLLOWED SELL — {ticker} sold {qty} @ {px}".

## Test surface (TDD)
- feature extractor: exit verbs detected; entry verbs unaffected.
- SellClassifier: exit verb + LLM is_sell → action='sell' + scope/fraction; no exit
  verb → no-op; low confidence / ticker-not-in-msg → no-op (fail-closed); an entry
  message → untouched.
- store: `get_open_shares_positions`, `remaining_qty` (nets trims + exits),
  `claim_exit` idempotency, `record_exit`.
- follow_sell_position: full sell of remaining; partial = floor(remaining×fraction);
  partial-fill cancels residual + records real qty; zero-fill releases claim;
  double-claim no-op; already-closed (remaining 0) no-op.
- SellFollower: action='sell' executes + returns skip(sell_followed); entry passes
  through; no matching position → skip with a distinct reason.
- migration: position_exits created on a legacy DB.
- e2e: filled shares intent → sell message → position sold, exit recorded, entry
  chain not run.

## Deferred (documented)
- **Options-leg sell-to-close** (needs `get_option_bid` + contract reconstruct).
- Multi-account / FIFO tax lots, reworded-partial double-trim (logged, not blocked).
- Other Phase E items (P&L attribution, replay harness, classifier eval,
  active-learning, economic guards) — separate specs.
