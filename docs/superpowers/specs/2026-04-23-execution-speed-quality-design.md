# Spec 2 — Execution Speed & Quality

**Date:** 2026-04-23
**Goal:** Get the first limit order live within ~6 seconds of a Discord signal, filled within
10 seconds of order placement, for any ticker including less-liquid names. No pre-warming
required. No blind market orders. Bounded aggressive walk with a hard chase cap.

---

## Problem

The current pipeline signal-to-order latency is 21–51 seconds:

| Bottleneck | Current cost |
|---|---|
| DesktopReader (screenshot + Opus vision) | 12–17s when triggered |
| TradeIntentDetector (Claude call) | 1–3s |
| TickerResolver (Claude call) | 1–3s |
| ConvictionClassifier (Claude call) | 1–3s |
| ChainLookup (qualifies full chain) | 10–30s |
| FillWaiter (passive wait) | up to 30s |

Discord signals move prices in seconds. By the time the current system places an order,
the best entry is often gone.

---

## Target Pipeline

```
t=0s:    Signal received via AX bridge
t=0.5s:  MessageNormalizer
t=1.5s:  Signal text extraction (AX or bounded screenshot, 1s cap)
t=2.5s:  SignalAnalyzer — 1 Haiku call (intent + ticker + side + conviction)
t=2.6s:  TickerValidator (deterministic)
t=2.6s:  ↳ chain params + spot price fetched IN PARALLEL
t=5.0s:  ChainLookup done (pre-filtered, ~3-4 contracts qualified)
t=5.5s:  ContractSelector, OrderSizer, OrderPricer
t=6.0s:  First walk step placed — order live
t=6–16s: PriceWalker running (fills within this window)
```

"Order live" target: ~6 seconds from signal. Expected gateway; actual timing depends on
Discord render state, IBKR gateway health, and market-data conditions.

---

## Section 1 — SignalAnalyzer: Collapse 3 LLM Calls to 1

### Current

`TradeIntentDetector`, `TickerResolver`, and `ConvictionClassifier` are three sequential
Claude API calls on the same message. Combined cost: 4–8 seconds.

### Change

A new `SignalAnalyzer` skill makes one Haiku call and returns a strict JSON schema:

```json
{
  "is_trade_signal": true,
  "ticker": "NVDA",
  "side": "long",
  "conviction": "high",
  "analysis_confidence": 0.91,
  "ambiguity_flags": [],
  "rationale": "Analyst initiating long position in NVDA calls"
}
```

**Enums enforced (parse failure on violation):**
- `side`: `long | short | none`
- `conviction`: `high | medium | low`
- `ambiguity_flags`: `ticker_implicit | multiple_tickers_detected | direction_unclear |
  non_actionable_commentary | slang_interpretation`

**Prompt constraints:** JSON only, no prose outside schema, structured output enforced.
Parse failure → `status: fail`, reason: `signal_parse_failed`.

**Ambiguity gate:** if `analysis_confidence < 0.70` or any flag present →
`execution_mode: observe`, `policy_state: ambiguous_signal`. Intent logged, no execution.

**Logical separation preserved:** `SignalAnalyzer` records `ticker_raw`, `side_raw`,
`conviction_raw` as separate fields on the intent row (see Spec 1) so per-concern error rates
(ticker extraction errors, side misclassification) are measurable independently.

### Deterministic Validation (TickerValidator)

The LLM proposes; a deterministic layer confirms. `TickerValidator` checks:
- Ticker resolves to a real IBKR contract (quick `qualify` call or allowed-universe check)
- Side is unambiguous
- If either fails → `policy_state: ambiguous_signal`, terminal

**The three existing skill classes** (`TradeIntentDetector`, `TickerResolver`,
`ConvictionClassifier`) are replaced by `SignalAnalyzer` + `TickerValidator` in the registry.

---

## Section 2 — Chain Lookup Speedup

### Current

`IBGateway.get_chain()` qualifies every contract in the full option chain then fetches quotes.
For a liquid stock: 600+ contracts × 2 IBKR calls each = 1200+ sequential API calls.
Cost: 10–30 seconds.

### Change

Pre-filter to policy criteria **before** any per-contract API calls. Apply selection logic to
the raw chain parameter data, qualify only the 4–6 surviving contracts.

**Revised flow inside `IBGateway.get_chain()`:**

```
Step 1 + 2 (parallel asyncio.gather):
  reqSecDefOptParamsAsync(ticker) → all expirations + strikes (1 IBKR call)
  get_quote(ticker)               → reference spot price + timestamp

Step 3 (in-process filter, zero IBKR calls):
  Strategy policy:   expiry ≥ min_expiry_days        [strategy config, not speed logic]
  Strike window:     3 strikes at/below spot (ITM) + 2 strikes above (ATM/OTM fallback)
                     matches existing closest_itm_call policy in ContractSelector
  Right:             calls only
  Result: 4–6 contracts

Step 4 (parallel asyncio.gather):
  For each surviving contract: qualifyContractsAsync + reqTickersAsync
  Partial-success semantics: drop failed contracts, keep successful
  Minimum viable threshold: if fewer than 2 valid candidates → fail with
    chain_lookup_insufficient_candidates
```

**Reference spot recorded on intent row:** `reference_spot_price`, `reference_spot_timestamp`.
Detectable staleness if ticker moved significantly during lookup.

**Important separation:**
- `min_expiry_days: 180` is **strategy policy** (in `policy.yaml`) — not baked into gateway logic
- Pre-filtering before qualification is the **performance optimization** — independent concern

**`ChainLookup` skill and `ContractSelector` skill are structurally unchanged.** The optimization
is internal to `IBGateway.get_chain()` only.

Expected improvement: low single-digit seconds vs. 10–30s current. Actual timing depends on
gateway responsiveness and market-data conditions.

---

## Section 3 — Signal Text Extraction

The AX bridge currently captures UI chrome (window titles, fragments) rather than actual Discord
message content. `DesktopReader` as currently implemented takes 12–17 seconds (AppleScript
navigation + full-screen screenshot + Opus vision call), which alone exceeds the 10-second target.

### Primary Path — Option A: Fix AX Tree Walking

`NotificationBannerClicker` already clicks the banner; Discord navigates to the channel.
`reconcile()` in `AXDiscordWatcher.swift` then walks the AX tree but targets
`kAXStaticTextRole` / `kAXTextAreaRole` — not the roles Discord (Electron) actually uses for
message text.

**Fix:** use Accessibility Inspector to identify correct AX roles/attributes for Discord message
content. Update `reconcile()` to target those roles. If successful: full message arrives via
socket, no screenshot, no vision model, expected low-latency path.

**AX content validation gate** (applied before passing text downstream):
- Length ≥ 40 characters
- Does not match window title / nav patterns (`丨`, `Stock Talk Insiders`, `#channel-name`)
- Optionally cross-checked against notification preview for basic consistency
- Validation failure → immediately drop to Option B

**Reliability threshold for permanent path selection:**
- AX extraction passes validation on >95% of sampled live notifications → keep as primary
- Below threshold → implement Option B as permanent fallback, remove Option A dependency

### Fallback Path — Option B: Bounded Screenshot Extraction (Degraded Mode)

Hard 1-second SLA. Treated as degraded mode, not an equivalent path.

- Channel navigation skipped (banner click already positioned Discord)
- Capture message-pane region only, not full screen
- Haiku for text extraction (not Opus)
- If 1-second SLA missed → continue immediately with notification preview text in context

**The trade path is never blocked waiting for full text.**

### Standardized Output (both paths)

```python
{
  "message_text": str,
  "source_mode": "ax" | "screenshot" | "preview_fallback",
  "extraction_confidence": float,   # 0.0–1.0
  "truncated": bool,
  "elapsed_ms": int
}
```

`SignalAnalyzer` receives this shape regardless of which path produced it.

### Decision Gate

Option A is attempted first during development. If AX message content is consistently reliable
(>95% threshold), Option B is not implemented. If AX proves brittle across Discord updates,
Option B is implemented as the permanent path.

---

## Section 4 — PriceWalker: Aggressive Bounded Walk

`OrderSubmitter` and `FillWaiter` are replaced by a single `PriceWalker` skill.

### Walk Profiles (configurable per channel)

```yaml
execution:
  walk_profile: aggressive_fast    # default; overridable per channel
  walk_profiles:
    cautious_fast:   [0.00, 0.02, 0.05, 0.10]
    aggressive_fast: [0.01, 0.03, 0.06, 0.10]
  reprice_interval_ms: 2500
  max_chase_pct: 0.15
```

Each value is a percentage buffer above the **live streaming ask at that step**. Steps do not
build on each other — each step re-anchors to the current live ask and adds its buffer.
All steps are hard-capped at `max_chase_price = initial_reference_ask × (1 + max_chase_pct)`.

### Step Timeline (aggressive_fast profile)

```
t=0s:    ask × 1.01   fills if market paused or near-stale
t=2.5s:  ask × 1.03   light chase
t=5s:    ask × 1.06   medium chase
t=7.5s:  ask × 1.10   hard chase (ceiling)
t=10s:   cancel → cancelled_unfilled
```

### Loop Logic Per Step

```
1. Read streaming ask from ib_insync event loop (instant, no API call)
   Guard: if quote age > 5s → terminate, cancel_reason: stale_quote

2. Compute limit = min(current_ask × (1 + step_buffer), max_chase_price)
   If limit == max_chase_price and step_buffer would exceed it:
     cancel_reason: price_exceeded_cap → cancelled_unfilled

3. Round UP to valid tick
   Source: contract metadata if available; otherwise $0.05 rule for non-penny-pilot names

4. Place limit order
   Record: order_submitted_at (first step only), order_ack_at, order_attempt_count (starts at 1)
   Update: initial_order_limit (first step), last_limit_price (every step)

5. Wait for fill
   PRIMARY:  ib_insync trade events (filledEvent / cancelledEvent)
   BACKUP:   0.5s polling loop as timeout backstop within reprice_interval_ms

6. If filled → record fill_price, filled_at, execution_state: filled. Done.

7. If not filled within reprice_interval_ms:
   Request cancel
   Wait for terminal state (Cancelled / Inactive) — no overlapping orders
   Advance to next step
```

### Terminal Outcomes

| State | cancel_reason | Routing |
|---|---|---|
| `filled` | — | Success |
| `cancelled_unfilled` | `walk_exhausted` | All steps ran within cap, no fill. Telegram alert. |
| `cancelled_unfilled` | `price_exceeded_cap` | Next step would breach cap, walk stopped early. Telegram alert. |
| `cancelled_unfilled` | `stale_quote` | Live ask too old to trust. Telegram alert. |
| `cancelled_unfilled` | `market_closed` | Execution window closed mid-walk. Telegram alert. |
| `cancelled_unfilled` | `fill_timeout` | Walk duration exceeded hard ceiling. Telegram alert. |
| `cancelled_unfilled` | `manual_cancel` | External cancellation. Telegram alert. |
| `failed` | — | Broker / API / system error. → DLQ + Telegram. |

`cancelled_unfilled` is not a DLQ event. Price ran away or walk exhausted is expected behavior
for a fast-moving Discord signal. `failed` is an operational problem requiring inspection.

### Latency Fields Updated by PriceWalker

All fields are on `trade_intents` (Spec 1):
- `order_submitted_at` — when first IBKR call is made
- `order_ack_at` — when IBKR returns trade object with orderId
- `order_attempt_count` — total cancel+replace cycles (1 = filled on first try)
- `last_limit_price` — final price in walk
- `filled_at` / `cancelled_at` — terminal timestamps

---

## New Skills / Changes

| Component | Change |
|---|---|
| `SignalAnalyzer` | New skill, replaces TradeIntentDetector + TickerResolver + ConvictionClassifier |
| `TickerValidator` | New skill, deterministic validation after SignalAnalyzer |
| `PriceWalker` | New skill, replaces OrderSubmitter + FillWaiter |
| `AXDiscordWatcher.swift` | Update reconcile() to target correct Discord AX roles |
| `DesktopReader` | Refactor: Option A primary, Option B bounded fallback, standardized output |
| `IBGateway.get_chain()` | Internal rewrite: pre-filter + parallel qualify |
| `registry.py` | Wire SignalAnalyzer + TickerValidator; remove 3 old skills; wire PriceWalker |
| `policy.yaml` | Add walk_profiles, walk_profile per channel, max_chase_pct |
| `ContractSelector` | Unchanged |
| `ChainLookup` | Unchanged (optimization is inside gateway) |

---

## What This Does Not Change

- Phase 1 pipeline structure
- Orchestrator
- StorageLayer (signal_store, execution_store, trace_store)
- IBGateway public interface (only `get_chain()` internals change)
- OrderSizer, OrderPricer (still run before PriceWalker, produce initial_reference_ask)
