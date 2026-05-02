# Per-Trader Conviction Classification — Design

**Date:** 2026-05-02
**Status:** Draft, awaiting user review

## Problem

The trading agent reads Discord messages from multiple traders (Stock Talk Weekly, Wall St Engine, UndefinedMystic, others) and must decide:

1. Is this an entry signal at all (vs. commentary, news, recap, exit)?
2. If yes, what conviction does the trader have?
3. Therefore: how big should the position be?

The current pipeline has two LLM skills (`SignalAnalyzer` and `ConvictionClassifier`) that use a generic phrase list ("strongly drawn", "full size", "nibbling") and a single global `low_conviction_pct` / `high_conviction_pct` mapping. This breaks down because:

- Each trader uses **different vocabulary** for the same conviction. WSE says "Added 2% pos." STW says "1% weighting." Mystic says "small swing trade."
- **Vocabulary drifts** — small message samples don't capture how a trader phrases things over months.
- Some traders **state size literally in the message** (WSE); others don't (Mystic).
- Some traders post **non-actionable content from the same account** (recaps, watchlists, macro commentary, replies, news-bot accounts).
- Per-trader signal hygiene (alert-mention conventions, bot accounts, channel tags) is uniform in code today but should be data.

## Goals

- **Latency-first.** Classification cannot block fast execution. Hot path must be < 1s message-to-intent for the LLM path, and ~10ms for the deterministic shortcut path.
- **Per-trader profiles in YAML.** New trader = new profile, no code change.
- **Robust to vocabulary drift.** Few-shot examples generalize beyond a fixed phrase list.
- **Fully autonomous in steady state.** No human in the hot path. Bootstrap-mode review is opt-in per trader for the first ~2 weeks.
- **Simple sizing.** Two buckets only: 5% or 10% of buying power. No tier zoo.
- **Auditable.** Every classification logs the features, model output, and the action taken.

## Non-Goals

- Thread-aware confirmation state machine (Mystic's "looks ready" pattern). Deferred to a follow-up spec.
- Reaction-emoji confidence weighting.
- Cross-trader endorsement signals (STW endorses Mystic).
- Multi-ticker fan-out from numbered watchlist messages. Deferred.
- P&L-based learning loop. Deferred.
- Replacing the existing `SignalAnalyzer` / `ConvictionClassifier` files in this spec — implementation will live alongside, with cutover planned in the implementation plan.

## Sizing — locked

Two buckets:

| Bucket | Size | When |
|---|---|---|
| **LOW** | 5% of buying power | Default for any actionable entry signal |
| **HIGH** | 10% of buying power | Only when the message is clearly high-conviction |
| **SKIP** | 0 | Commentary, news, exits, watchlist, sympathy, ambiguous below threshold |

If the trader **states a size in the message** (e.g., "Added 2% pos."), use that stated size, capped at 10%. Stated size always wins over the bucket — it's the trader's own number.

## Architecture

Two paths:

```
                                 ┌────────────────┐
  Discord message  ──────────►   │  Fast path     │  ──►  TradeIntent
  (already in DB)                │  (sub-second)  │
                                 └────────┬───────┘
                                          │
                                  classification_log
                                          │
                                          ▼
                                 ┌────────────────┐
                                 │  Cold path     │
                                 │  (nightly,     │
                                 │   off-thread)  │
                                 └────────┬───────┘
                                          │
                                          ▼
                                  trader_examples_pending
                                  (you approve, written
                                   back to YAML profile)
```

### Fast path — single skill, single LLM call

A new skill `TraderClassifier` replaces the current `SignalAnalyzer` + `ConvictionClassifier` pair. One LLM call returns ticker, side, bucket (LOW / HIGH / SKIP), confidence, reason. Steps:

1. **Author/bot filter** (deterministic, ~0ms). Skip if author matches `bot_authors_to_skip` for the trader.
2. **Alert-mention check** (deterministic). If `require_alert_mention: true`, skip if the trader's alert role isn't mentioned.
3. **Availability gate** (deterministic). Skip if `now < trader_unavailable_until`.
4. **Feature extraction** (regex, ~5ms). Extract:
   - `stated_size_pct` — first match of `(\d+(?:\.\d+)?)%\s*(?:pos|weighting|position)?`
   - `entry_verb_present` — boolean: `open|opening|added|bought|initiating|joining|loading|took|grabbed|picked up|started|scaled in`
   - `tickers_in_msg` — all `\$[A-Z]{1,6}` matches
   - `embed_present` — boolean
   - `msg_length`
   - `is_thread_reply` — boolean
   - `availability_phrase_match` — for off-grid signals
5. **Deterministic shortcut.** If `stated_size_pct` is present AND `entry_verb_present` AND exactly one ticker:
   - Use stated size directly (capped at 10%).
   - Set `bucket = HIGH` if stated_size ≥ 5% else `LOW` (purely for downstream record-keeping).
   - **Fire LLM call asynchronously** for audit-only — does not block. Shortcut path target: <50ms.
6. **LLM classify** (one call, prompt-cached). System prompt = trader profile + few-shot examples. Cached via `cache_control: ephemeral`. User content = message + extracted features. Model: `claude-haiku-4-5` (fast).
   - Returns: `{ is_entry, ticker, side, bucket, confidence, reason }`.
7. **Confidence routing:**
   - `confidence >= 0.80` → fire bucket size as classified.
   - `0.50 <= confidence < 0.80` → **downgrade**: fire at LOW (5%) regardless of classified bucket. (Half-size substitute, simpler than fractional sizing — keeps the 5/10 guarantee.)
   - `confidence < 0.50` → drop, log for cold-path review.
8. **Update availability state** if the message contained an availability phrase. (`trader_unavailable_until` set to phrase-implied window, e.g., 7 days for "off the grid", 14 days for "passover/vacation".)
9. **Bootstrap mode override.** If trader's `auto_execute: false`, do not write a `TradeIntent`; instead post the proposed classification to Telegram with the trader's name, message excerpt, bucket, confidence, reason. Telegram reactions append to `trader_examples_pending`. Bootstrap-mode messages do not fire trades.

### Cold path — runs as a scheduled job, not in the request path

Daily (or on-demand):

- Read `classification_log` for the past 24h.
- Surface low-confidence and confidence-near-threshold messages.
- For each, propose a tier with reasoning, write to `trader_examples_pending`.
- A separate review tool (terminal or scheduled agent) lets you approve → appends to the trader's YAML `conviction_examples` array.
- Profile reload is hot — picked up on next message without restart.

Cold path is intentionally simple in v1: surface and queue. P&L feedback and auto-promotion of examples are out of scope.

## Per-Trader Profile

Profiles live in `config/traders/<handle>.yaml`. Loaded at startup and on file change.

```yaml
# config/traders/wallstengine.yaml
handle: wallstengine
display_name: Wall St Engine
discord_author_pattern: "Wall St Engine"      # exact match on Discord author display name
alert_mention: "@Wall - Alerts"
require_alert_mention: true
bot_authors_to_skip: ["WSE"]                  # APP-tagged news bot
auto_execute: true                             # phase 2 — fully autonomous
size_in_message: true                          # WSE writes "X% pos."
prefer_message_size: true                      # use stated size when present
classifier_model: claude-haiku-4-5
availability_phrases: []
conviction_examples:
  - msg: "Added small AUDC (speculative play) 2% pos. on back of BAND earnings"
    bucket: LOW
    why: "speculative play, small, 2%"
  - msg: "OPEN $SHEN ... taking a stab at SHEN around 21DMA ahead of earnings Friday"
    bucket: LOW
    why: "stab/small horizon framing"
  - msg: "Added a 2% position in CEG calls. After a month of consolidation, CEG looks like it's setting up for a move here."
    bucket: LOW
    why: "2% explicit, standard add"
  - msg: "PORTFOLIO UPDATE - 18 POSITIONS ..."
    bucket: SKIP
    why: "portfolio recap, not an entry"
  - msg: "FDA APPROVES ARVINAS' $ARVN VEPDEGESTRANT"
    bucket: SKIP
    why: "WSE bot news headline (also caught by bot filter)"
```

```yaml
# config/traders/stocktalkweekly.yaml
handle: stocktalkweekly
display_name: Stock Talk Weekly
discord_author_pattern: "Stock Talk Weekly"
alert_mention: "@Stock Talk Weekly - Alerts"
require_alert_mention: true
bot_authors_to_skip: []
auto_execute: false                            # phase 1 — review-bootstrap until 15+ approved examples
size_in_message: true
prefer_message_size: true
classifier_model: claude-haiku-4-5
availability_phrases: ["off the grid", "passover", "on vacation"]
conviction_examples:
  - msg: "This will join the 'Prospective Positions' group with a small 1% weighting @ $41.22"
    bucket: LOW
    why: "explicit 1%, prospective starter"
  - msg: "names that I want to upsize when the opportunity arises"
    bucket: SKIP
    why: "intent to upsize later, not an entry now"
  - msg: "Fully closed remainder of position. ... lowest conviction name in the portfolio"
    bucket: SKIP
    why: "exit, not entry"
  - msg: "PORTFOLIO UPDATE - 18 POSITIONS ..."
    bucket: SKIP
    why: "ledger snapshot"
  - msg: "Yet another intraday fade in the market with $SPY $QQQ flushing red..."
    bucket: SKIP
    why: "macro commentary"
```

```yaml
# config/traders/mystic.yaml
handle: mystic
display_name: UndefinedMystic
discord_author_pattern: "UndefinedMystic"
alert_mention: "@Alerts - Mystic"
require_alert_mention: true
bot_authors_to_skip: []
auto_execute: false                            # phase 1 — narrative-heavy trader, bootstrap first
size_in_message: false                         # rarely states %
prefer_message_size: true                      # but use it if present
classifier_model: claude-haiku-4-5
availability_phrases: ["off the grid", "passover", "on vacation", "traveling"]
conviction_examples:
  - msg: "i opened a small swing trade position in INDI ... short term swing trade based on momentum"
    bucket: LOW
    why: "small + swing trade self-label"
  - msg: "bought todays IPO $ELMT here at $17.90 for a swing trade"
    bucket: LOW
    why: "swing trade framing, single-shot"
  - msg: "Alpha + Omega Semiconductor long idea\n[multi-paragraph thesis with 8+ bullet points]"
    bucket: HIGH
    why: "labeled 'long idea', deep multi-point thesis, structural bull case"
  - msg: "These are mid level conviction swing trade ideas for current mkt regime"
    bucket: LOW
    why: "self-stamped mid-level swing"
  - msg: "fyi.... INTTeresting given nxpi earnings move today"
    bucket: SKIP
    why: "fyi/interesting, no entry"
  - msg: "tailwind for the apple peice...."
    bucket: SKIP
    why: "color, not entry"
  - msg: "looks ready"
    bucket: SKIP
    why: "thread-confirmation candidate, but thread-aware logic out of scope; treat as SKIP for now"
```

## Data Model

### `traders` config (filesystem)

`config/traders/*.yaml`. Each file is one trader. Loaded into memory at startup and watched for changes.

### `classification_log` (new SQLite table in `agent.db`)

```sql
CREATE TABLE classification_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  trader_handle TEXT NOT NULL,
  msg_text TEXT NOT NULL,
  features_json TEXT NOT NULL,            -- extracted features (regex)
  llm_response_json TEXT,                 -- raw LLM output (null if shortcut path)
  bucket TEXT NOT NULL,                   -- LOW | HIGH | SKIP
  confidence REAL NOT NULL,               -- 0–1
  size_pct REAL NOT NULL,                 -- 0.05, 0.10, or stated; 0 for SKIP
  size_source TEXT NOT NULL,              -- shortcut_stated | bucket_low | bucket_high | downgrade | skip
  action_taken TEXT NOT NULL,             -- fired | bootstrap_review | skipped | dropped_low_conf
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_classification_log_trader_time ON classification_log(trader_handle, created_at);
```

### `trader_examples_pending` (new SQLite table)

```sql
CREATE TABLE trader_examples_pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trader_handle TEXT NOT NULL,
  msg_text TEXT NOT NULL,
  proposed_bucket TEXT NOT NULL,
  proposed_why TEXT,
  source TEXT NOT NULL,                   -- low_confidence | bootstrap_review | manual
  status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_bucket TEXT
);
```

### `trader_state` (new SQLite table)

```sql
CREATE TABLE trader_state (
  trader_handle TEXT PRIMARY KEY,
  unavailable_until TEXT,                 -- ISO timestamp or NULL
  updated_at TEXT NOT NULL
);
```

## Pipeline Integration

The current pipeline runs `SignalAnalyzer → TickerResolver → TickerValidator → TradeIntentDetector → ConvictionClassifier → OrderSizer → TradeIntentWriter` (approximately).

The new flow:

```
TraderRouter         → resolves discord author → trader profile (or drops)
TraderClassifier     → replaces SignalAnalyzer + ConvictionClassifier
                       outputs: ticker, side, bucket, confidence, size_pct
TickerResolver       → unchanged
TickerValidator      → unchanged
OrderSizer           → simplified: reads size_pct directly from ctx, no more high/low table
TradeIntentWriter    → unchanged (writes intent), bootstrap-mode short-circuits before this
```

Notable simplifications downstream:

- `OrderSizer` no longer reads `sizing_policy.high_conviction_pct` / `low_conviction_pct`. It reads `ctx["size_pct"]` set by `TraderClassifier`.
- `SizingPolicy` in `agent/policy.py` becomes unused for new traders; kept for backward compatibility until cutover, then removed.

## Telegram Bootstrap Channel

Reuses existing Telegram digest infrastructure. New bootstrap-review message format:

```
[mystic | bootstrap]
"i opened a small swing trade position in INDI..."
Proposed: LOW @ 5% (confidence 0.72)
Why: "small + swing trade self-label"

React: 👍 = approve LOW   ⬆️ = upgrade to HIGH   ⬇️ = SKIP   ❌ = reject example
```

Reactions write to `trader_examples_pending` with the chosen bucket. A nightly job promotes approved entries into the relevant trader YAML and removes them from the pending table.

When a trader's profile reaches **15 approved examples** (threshold configurable), the bootstrap reviewer posts a one-time *"ready for autonomous"* message with a one-tap toggle that flips `auto_execute: true`.

## Latency Budget

| Stage | Budget |
|---|---|
| Author/mention/availability filters | < 5ms |
| Feature extraction (regex) | < 10ms |
| Shortcut path (stated size + entry verb) | total < 50ms |
| LLM call (Haiku, prompt-cached) | 300–600ms typical |
| `TradeIntent` write | existing |
| **Total fast-path overhead** | **~10ms (shortcut)** to **~600ms (LLM)** |

Prompt caching is essential — the trader's profile + 5–10 examples is ~2–4k tokens. Without caching, each call re-pays that cost. With `cache_control: ephemeral`, steady-state calls only pay for the new message + response.

## Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| LLM returns malformed JSON | Reuse existing `_safe_json` fallback. If still unparseable, log + drop. |
| LLM returns ticker not present in message | Validate against `tickers_in_msg`; if mismatch, drop. |
| Trader posts a new phrasing the LLM mishandles | Confidence drops → 5% downgrade → cold path queues for review. |
| Trader's account compromised / impersonator | Out of scope; would need signed message verification. |
| Profile YAML malformed | Fail loud at startup; do not silently fall back. |
| Two messages arrive simultaneously for same ticker | Existing dedupe policy handles this; classifier itself is stateless per message. |
| Stated size > 10% (e.g., trader says "20% pos") | Cap at 10%. Log a warning. |
| Availability phrase false-positive ("off the grid for 5 minutes during this trade") | 7-day default may over-skip; mitigated by trader-specific tuning of phrase list. Acceptable risk: errs toward not trading rather than wrong-trading. |

## Migration Plan

1. Build `TraderClassifier`, `TraderRouter`, profile loader alongside existing skills.
2. Run **shadow-mode**: classify each incoming message with both old and new pipelines, log divergences. No execution change.
3. After 1 week of shadow data, review divergences, tune profiles.
4. Cut over per trader, starting with WSE (clearest grammar, highest auto_execute readiness).
5. Bootstrap STW and Mystic in `auto_execute: false` mode.
6. Once each trader hits 15 approved examples, flip `auto_execute: true`.
7. Remove old `SignalAnalyzer` + `ConvictionClassifier` once all traders cut over.

## Open Questions for User Review

1. **Availability gate strictness.** A "passover" or "off the grid" mention currently sets a 7–14 day skip window. Is that acceptable, or should availability be advisory (downgrade HIGH→LOW) rather than hard-skip?
2. **Bootstrap example threshold.** 15 approved examples before auto-execute. Too many? Too few?
3. **Stated-size trust ceiling.** Cap at 10%, or trust the trader's stated size up to a higher absolute (e.g., 15%) for HIGH-confidence cases?
4. **Confidence cutoff for `LOW` downgrade.** Currently `< 0.80` downgrades to LOW (5%). Keep, or set the cutoff per trader?

## Acceptance Criteria

- [ ] One `TraderClassifier` skill replaces both legacy classifiers in the live pipeline.
- [ ] At least three trader profiles (`wallstengine`, `stocktalkweekly`, `mystic`) loaded from `config/traders/*.yaml`.
- [ ] Sizing reduced to two buckets: 5% and 10%, plus stated-size override capped at 10%.
- [ ] All classifications logged to `classification_log` with features, model output, bucket, confidence, action.
- [ ] Bootstrap (Telegram review) mode works for `auto_execute: false` traders; reactions append to `trader_examples_pending`.
- [ ] WSE-style "Added X% pos." messages take the deterministic shortcut path (verified by trace).
- [ ] Hot-path P50 latency: shortcut < 50ms; LLM path < 800ms (P50), < 1500ms (P99).
- [ ] Profile YAML reload is hot — no agent restart required.
- [ ] Existing `SignalAnalyzer` + `ConvictionClassifier` removed only after full cutover.
