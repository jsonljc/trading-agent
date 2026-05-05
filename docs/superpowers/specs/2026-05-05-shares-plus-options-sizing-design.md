# Shares-First + Options Sleeve, Per-Channel Sizing — Design

**Status:** Draft
**Author:** Jason (with Claude)
**Date:** 2026-05-05
**Supersedes:** [`2026-05-04-shares-only-entry-with-trim-ladder-design.md`](2026-05-04-shares-only-entry-with-trim-ladder-design.md) — the trim ladder behavior carries over unchanged; sizing matrix and entry chain are replaced.

## Problem

Three independent issues motivate this revision:

1. **Sizing is wrong.** The current code (`skills/signal/trader_classifier.py:17-18`) sizes 5%/10% off **`BuyingPower`**, which IB inflates by the margin multiplier. Effective deployment is 10%/20% of cash on a 2× margin account, not the intended 5%/10%. The 2026-05-04 spec doubled down on this by raising sizing to 40%/80% of `BuyingPower` — that would deploy 80%/160% of *cash* per signal, which is far more aggressive than intended.
2. **Options-only fills.** Today's path (`policy.yaml:5` `prefer_options: true`, line 8 `fallback_to_stock_if_no_options: true`) goes options-first, falls back to shares only on lookup failure. In recent paper trades, every entry was options-only — no shares ever fired. The user wants the inverse: shares first, options as an additive sleeve, with a chase guard so we don't pay up for the options leg when the move has already happened.
3. **WSE 3%-stated bucketed HIGH.** WSE explicitly stated "3% pos" (a small add). The classifier's deterministic shortcut (`trader_classifier.py:67-89`) requires a single ticker, and WSE often mentions multiple tickers per message — so the shortcut fails, the LLM runs, and the LLM read the structured thesis as HIGH. Result: a "3%, casual add" message produced a HIGH-conviction order. Stated size should always win over LLM thesis-quality reading when the trader explicitly signals "small."

Channel-ID corrections: separately, the user has determined that `1229546005788098580` (currently mapped to `mystic` in `policy.yaml:100`) is actually `stock-talk-portfolio`, and the previous STP id `1217309136681832540` is actually `mystic`. The `signal_events` table confirms this — rows labeled `channel='mystic'` have `author='Stock Talk Weekly'`, and rows labeled `channel='stock-talk-portfolio'` have `author='UndefinedMystic'`. The IDs were swapped.

## Goal

Replace the entry path with:
- **Shares first**, then a 5%-of-base **options sleeve** gated by a 10% chase guard against the reference price.
- **Per-channel sizing table** (not module-level constants), with `stock-talk-portfolio` as the highest-conviction trader and a fallback default for new channels.
- **Sizing base = `NetLiquidation × margin_multiplier`** (default 2.0, configurable). Stable denominator across the day — does not shrink as positions are deployed (matches user's "of total cash, not total cash remaining" requirement).
- **WSE-style fix:** when the message contains an explicit small stated size (`< 7.5%`), force the bucket to LOW regardless of LLM verdict.
- **Channel ID swap:** correct `mystic` ↔ `stock-talk-portfolio` IDs and add `urkel` + `pup-danny` mappings.

Out of scope (deferred): partial-fill recovery, automated options exit (held to expiry or sold manually for v1), short signals (still dropped at submitter as in the prior spec), live trading (paper only — `_assert_paper_guard` remains).

## Behavior

### Sizing matrix

Sizing base = `NetLiquidation × margin_multiplier`, default `margin_multiplier = 2.0`. Computed once per signal at classification time.

| Channel | Bucket | Shares % | Options % | Total deployed |
|---|---|---|---|---|
| stock-talk-portfolio | HIGH | 20 | 5 | 25 |
| stock-talk-portfolio | LOW  | 15 | 5 | 20 |
| mystic               | HIGH | 15 | 5 | 20 |
| mystic               | LOW  | 10 | 5 | 15 |
| wallstengine, urkel, pup-danny (default) | HIGH | 10 | 5 | 15 |
| wallstengine, urkel, pup-danny (default) | LOW  |  5 | 5 | 10 |

The "default" row is the fallback for any channel without an explicit override. New channels added later (e.g., a sixth trader) default to this until they earn an explicit row in the table.

### Entry sequence (per signal)

```
TraderClassifier  →  bucket ∈ {HIGH, LOW, SKIP}; SKIP terminates here
       ↓
TradeIntentWriter, ChannelPolicyGuard, CooldownGuard, ExecutionEligibilityGuard, RthEntryGuard
       ↓ (carried over from 2026-05-04 spec; RthEntryGuard skips premarket/afterhours)
ReferencePriceCapture  →  ctx["reference_price"] = gateway.get_quote(ticker)
       ↓
SizingResolver  →  reads channel + bucket from ctx,
                   computes shares_pct + options_pct from policy table,
                   sets ctx["shares_alloc"] and ctx["options_alloc"]
       ↓
EquityContractBuilder  →  qualified STK contract  (from 2026-05-04 spec)
       ↓
SharesMarketSubmitter  →  drop if side=="short"; else MKT BUY shares,
                          wait for fill, persist fill_qty/fill_price,
                          INSERT R1+R2 into trade_intent_trims (armed)
       ↓
OptionsChaseGuard  →  re-quote current_price.
                       if current_price > reference_price × 1.10 → skip
                       (log "options_chase_skip", emit telegram digest, done)
                       else continue
       ↓
ChainLookup, InstrumentMarketabilityGuard, ContractSelector  (existing, options branch)
       ↓
OptionsMarketSubmitter  →  closest ITM call, ≥180 DTE, MKT BUY,
                            5% of sizing_base allocation,
                            wait for fill, persist as second trade_intents row
                            with parent_intent_id pointing at the shares row
       ↓
TelegramDigest  →  posts shares fill + options fill (or skip reason)
```

Two `trade_intents` rows are written per signal: one for shares (`instrument_type='equity'`, with trim-ladder armed), one for options (`instrument_type='option'`, no exit ladder for v1). The options row references the shares row via a new `parent_intent_id` column.

If the shares submission fails (rejection, no liquidity), the options leg does not fire. We do not place an options-only entry as a fallback.

### Reference price for chase guard

`reference_price` is captured **before** the shares MKT submits, by calling `gateway.get_quote(ticker)`. This is the trader's edge moment — the price at which the signal was processed. The chase guard compares the post-shares-fill quote against this same reference price. The shares fill itself may have moved the market, especially on thin names; we still compare against the earlier reference, not the shares fill price, because the signal-time price is what the trader saw.

If `current_price > reference_price × 1.10`, options leg is skipped with `options_chase_skip`. Logged in the telegram digest. The shares position remains as-is (no reversal), and the trim ladder still arms on the shares.

### Trim ladder (carried over unchanged)

R1 at +5% over shares avg fill: sell 40% of original filled shares qty, MKT.
R2 at +10% over shares avg fill: sell another 40% of original filled qty, MKT.
20% tail holds forever (no further automation, no stop). Same logic as the 2026-05-04 spec.

The trim ladder operates on **shares only**. The options leg is not laddered. For v1, options are held to expiry or closed manually if needed.

### Classifier WSE-fix

`skills/signal/trader_classifier.py`, after the shortcut path (which already forces LOW on stated_size < 7.5% when shortcut conditions match):

After the LLM responds with `bucket=HIGH` (or any actionable bucket) but `features.stated_size_pct is not None and features.stated_size_pct < 7.5`, force `bucket = "LOW"` and tag the result with `size_source="wse_small_size_override"`. Log the override.

This catches WSE's multi-ticker 3% messages that bypass the shortcut. The trader's explicit "small position" wins over the LLM's thesis read.

The `MAX_STATED_SIZE = 0.10` cap (`trader_classifier.py:19`) becomes dead under the new design — stated_size_pct only picks the bucket, not the size. Remove it.

The shortcut path also changes: `size_pct` is no longer derived from `stated_size_pct / 100` (lines 74). Instead, the shortcut sets the bucket and the per-channel sizing table sets `size_pct` downstream (in `SizingResolver`). The shortcut still exists — it lets us skip the LLM call when the message is unambiguous — but the size determination moves to the policy table.

### Short signals (carried over unchanged)

`SharesMarketSubmitter` drops `side="short"` with `unsupported_short_signal`. Options sleeve also doesn't fire. Future spec for shorts is its own design.

## Architecture

### What changes

**Modified:**
- `skills/signal/trader_classifier.py`:
  - Remove `SIZE_LOW`, `SIZE_HIGH`, `MAX_STATED_SIZE`, `SIZE_HIGH_SHORTCUT_THRESHOLD` constants. Keep `HIGH_CONF_THRESHOLD = 0.80`, `DROP_CONF_THRESHOLD = 0.50`.
  - Shortcut path: still triggers on `prefer_message_size + stated_size + entry_verb + single ticker`, but no longer sets `size_pct`. It only sets `bucket` (`HIGH` if stated ≥ 7.5%, else `LOW`).
  - LLM path: after LLM responds, apply WSE-fix override — if `stated_size_pct is not None and stated_size_pct < 7.5`, force `bucket = "LOW"` with `size_source="wse_small_size_override"`.
  - Remove all `size_pct` writes from this skill. `size_pct` is set by `SizingResolver` later.
- `skills/execution/order_sizer.py`:
  - Replace `account.buying_power * size_pct` with `account.net_liquidation * margin_multiplier * size_pct`.
  - Read `margin_multiplier` from `policy.execution.margin_multiplier`.
  - **Read `size_pct` based on `instrument_type`**: equity branch reads `ctx["shares_pct"]`, option branch reads `ctx["options_pct"]`. Both keys are populated by `SizingResolver` upstream. The skill is reused unchanged in both sub-chains; only the source key changes.
- `agent/policy.py`:
  - Add `ExecutionPolicy.margin_multiplier: float = 2.0`.
  - Add `ExecutionPolicy.sizing: SizingPolicy` (new model, see below).
  - Drop `instrument_policy.fallback_to_stock_if_no_options` (dead under shares-first). Mark deprecated; loader ignores if present.
- `agent/registry.py:build_phase2b_execution_chain`:
  - Replace with the new chain shown in "Entry sequence" above.
  - The shares sub-chain (`EquityContractBuilder` → `OrderSizer` → `SharesMarketSubmitter`) and the options sub-chain (`ChainLookup` → `InstrumentMarketabilityGuard` → `ContractSelector` → `OrderSizer` → `OptionsMarketSubmitter`) are sequential within a single chain — the options sub-chain is gated by `OptionsChaseGuard`, which terminates the chain if the chase threshold is exceeded.
- `infra/ib/gateway.py:place_order`: branch on `PreparedOrder.order_type` ∈ {`"LMT"`, `"MKT"`} (already in 2026-05-04 spec). Carry over.
- `infra/ib/models.py:PreparedOrder`: `limit_price: float | None` (already in 2026-05-04 spec). Carry over.
- `skills/execution/trade_intent_writer.py`: write two intent rows per signal (shares + options), linked via `parent_intent_id`. Set `instrument_type="equity"` for the shares row, `"option"` for the options row.
- `config/policy.yaml`: see "Configuration" section below for the diff.
- `infra/storage/db.py`: add `parent_intent_id TEXT` column to `trade_intents` (additive ALTER), with FK to `trade_intents(intent_id)`. Existing rows have `parent_intent_id = NULL`.

**New:**
- `skills/execution/reference_price_capture.py` — calls `gateway.get_quote(ticker)`, stores in `ctx["reference_price"]`. Runs early, before any order placement. If quote fails, abort the chain (we cannot enforce the chase guard without a baseline). Log and skip the signal.
- `skills/execution/sizing_resolver.py` — looks up `(channel, bucket)` in `policy.execution.sizing`, sets `ctx["shares_pct"]` and `ctx["options_pct"]`. Falls back to the `default` row if channel not found.
- `skills/execution/options_chase_guard.py` — re-quotes `gateway.get_quote(ticker)` and compares to `ctx["reference_price"] × 1.10`. If exceeded, returns `SkillResult(status="skip", reason="options_chase_skip")` which terminates the chain at this skill (no options orders placed). The shares position and trim ladder are unaffected.
- `skills/execution/shares_market_submitter.py` — already specified in 2026-05-04. Drops `side="short"`. Submits shares MKT, waits for fill, persists fill_qty/fill_price, arms trim rungs in `trade_intent_trims`.
- `skills/execution/options_market_submitter.py` — new sibling of `SharesMarketSubmitter`. Submits options MKT, waits for fill, persists to `trade_intents` with `instrument_type='option'` and `parent_intent_id` pointing at the shares row. Does NOT arm trim ladder.
- `skills/execution/equity_contract_builder.py` — already specified in 2026-05-04. Carry over.
- `skills/execution/rth_entry_guard.py` — already specified in 2026-05-04. Carry over.
- `agent/exit_ladder.py` — already specified in 2026-05-04. Carry over.
- `config/traders/urkel.yaml`, `config/traders/pup-danny.yaml` — new minimal trader profiles. `auto_execute: true`, empty `conviction_examples: []` (LLM runs without trader-specific anchors until examples are added). Author pattern matches the channel handle.

**Unchanged but still in the entry chain (for the options sub-chain):**
- `skills/execution/chain_lookup.py`, `instrument_marketability_guard.py`, `contract_selector.py` — these are now used for the **options leg only**, gated by `OptionsChaseGuard`. The shares leg uses `EquityContractBuilder` directly.
- `skills/execution/order_pricer.py`, `price_walker.py` — bypassed entirely. Both legs are MKT now. Leave the files in place (cleanup is YAGNI for v1).

### Component boundaries

The two sub-chains (shares-first, then options-if-not-chased) live inside one execution chain. Each skill returns `SkillResult(status="success" | "fail" | "skip")`. `OptionsChaseGuard` returning `skip` terminates the chain after the shares leg has fully completed — no options orders place, but the shares fill and trim arming are persisted. This matches the existing skill-chain pattern.

## Data Model Changes

```sql
-- Additive: link options orders to their parent shares order
ALTER TABLE trade_intents ADD COLUMN parent_intent_id TEXT;

-- (no foreign key in SQLite ALTER; enforced at write time)

-- The trade_intent_trims table from 2026-05-04 spec carries over unchanged.
-- The fill_qty column added in that spec carries over unchanged.
```

`parent_intent_id` is NULL for shares orders (they are their own parent) and set to the shares `intent_id` for options orders. This lets us join shares + options for reporting without changing any existing query.

## Configuration

`config/policy.yaml` diff:

```yaml
instrument_policy:
  prefer_options: true               # repurposed: true = shares-first + options sleeve;
                                     #             false = shares-only (skip options leg)
  min_expiry_days: 180
  strike_policy: closest_itm_call
  # fallback_to_stock_if_no_options: REMOVED (dead under shares-first)

execution:
  # ...existing keys...
  margin_multiplier: 2.0             # NEW. Reg-T overnight = 2.0; PDT day-trade = 4.0.
  exit_poll_interval_seconds: 2      # from 2026-05-04 spec (shares trim ladder)
  trim_ladder:                       # from 2026-05-04 spec
    rungs:
      - threshold_pct: 0.05
        trim_pct: 0.40
      - threshold_pct: 0.10
        trim_pct: 0.40
  options_chase_threshold_pct: 0.10  # NEW. Skip options if current_price > ref × 1.10.
  sizing:                            # NEW per-channel sizing table.
    default:
      high: { shares: 0.10, options: 0.05 }
      low:  { shares: 0.05, options: 0.05 }
    per_channel:
      stock-talk-portfolio:
        high: { shares: 0.20, options: 0.05 }
        low:  { shares: 0.15, options: 0.05 }
      mystic:
        high: { shares: 0.15, options: 0.05 }
        low:  { shares: 0.10, options: 0.05 }

discord_extension:
  forwarder_port: 9876
  channel_id_map:
    "1229546005788098580": stocktalkweekly      # was: mystic (CORRECTED)
    "1217309136681832540": mystic               # was: stock-talk-portfolio (CORRECTED)
    "1248378121451733083": wallstengine         # unchanged
    "1221605346305642558": pup-danny            # NEW
    "1151611275709788253": urkel                # NEW
```

`watched_channels` already lists `urkel` and `pup-danny` with `auto_execute: true` (lines 55–58 of current policy.yaml); no edit there.

Note on the channel-id-map handles: the values must match the `handle` field of the corresponding trader profile YAML. STP's profile (`config/traders/stocktalkweekly.yaml`) has `handle: stocktalkweekly`, so the map value is `stocktalkweekly`, not `stock-talk-portfolio`. (The user-facing channel name in `watched_channels` is `stock-talk-portfolio`; the routing handle inside the trader registry is `stocktalkweekly`. These are not interchangeable.) This is consistent with current code — `TraderRouter` looks up by handle, not by channel name.

### New trader profiles

`config/traders/urkel.yaml`:
```yaml
handle: urkel
display_name: Urkel
discord_author_pattern: "Urkel"
alert_mention: ""
require_alert_mention: false
bot_authors_to_skip: []
auto_execute: true
size_in_message: false
prefer_message_size: false
classifier_model: claude-haiku-4-5
availability_phrases: []
conviction_examples: []
```

`config/traders/pup-danny.yaml`: same structure with `handle: pup-danny`, `display_name: Pup Danny`, `discord_author_pattern: "Pup Danny"`. Both start with empty conviction_examples; the LLM classifier runs without trader-specific anchors. Once a few weeks of messages are observed, examples can be added (similar to how mystic/STW/WSE were curated).

## Behavior Details

### Idempotency

Per-leg idempotency keys (extending the 2026-05-04 pattern):
- Shares submit: `f"{trace_id}:SharesMarketSubmitter:{event_id}"`
- Options submit: `f"{trace_id}:OptionsMarketSubmitter:{event_id}"`
- Trim sells: `f"{intent_id}:trim:R{rung}"` (carried over from 2026-05-04)

A duplicate signal arriving twice writes one shares row and one options row (or zero of each) — never two of either, because the idempotency keys dedupe at the gateway boundary.

### Restart recovery

Same as 2026-05-04 spec for the shares trim ladder. Options leg has no in-flight state to recover — once submitted and filled, it's just a passive position.

If the agent crashes between shares fill and options submit, the shares position is in the account with the trim ladder armed; the options leg simply does not fire. No retry. This is acceptable: missing the options sleeve on a single signal is preferable to placing two options orders on restart.

### Failure modes

| Failure | Behavior |
|---|---|
| Shares MKT rejected | Mark intent `failed`, do NOT arm trims, do NOT submit options. |
| Shares MKT partial fill | Arm trims on the actually-filled qty. Options leg fires normally (5% of base, not adjusted for partial). |
| Reference price quote fails | Abort the chain. Cannot enforce chase guard without it. Log `reference_price_unavailable`, no orders placed. |
| Options chase guard tripped (`current > ref × 1.10`) | Shares leg already filled and trims armed; options leg skipped with `options_chase_skip`. Telegram digest reports both. |
| Options chain lookup fails (no options listed) | Skip options leg with `no_options_chain`. Shares leg unaffected. |
| Options MKT rejected | Mark options intent `failed`, do NOT retry. Shares + trims unaffected. |
| Concurrent BP race (two HIGH signals milliseconds apart) | First signal places shares + options; second signal's shares may reject for insufficient buying_power. Accepted — see 2026-05-04 spec, "Concurrent BP race." |

### WSE-fix edge cases

| Message | Stated size | Shortcut path? | Bucket result |
|---|---|---|---|
| "Added 2% AUDC speculative" | 2% | Yes (single ticker, entry verb) | LOW (shortcut <7.5%) |
| "Added 3% CEG, paired with VST" | 3% | No (multi-ticker) | LOW (LLM HIGH → forced to LOW by override; size_source=`wse_small_size_override`) |
| "OPENING $SEI 5% pos. Catalyst..." | 5% | Yes if STW (single ticker) | LOW |
| "10% pos in $XYZ catalyst story" | 10% | Yes | HIGH (≥7.5%) |
| "OPEN AMBQ structured thesis" | None | No (no stated size) | LLM verdict applies (no override) |
| "$AAPL [no entry verb]" | 5% | No (no entry verb) | LLM verdict; if HIGH and stated <7.5%, override to LOW |

The override fires only when `stated_size_pct is not None and stated_size_pct < 7.5`. Messages without a stated size flow through unchanged.

### Configuration validation

Pydantic model for `SizingPolicy`:
```python
class SizingTier(BaseModel):
    shares: float = Field(ge=0.0, le=1.0)
    options: float = Field(ge=0.0, le=1.0)

class SizingBuckets(BaseModel):
    high: SizingTier
    low: SizingTier

class SizingPolicy(BaseModel):
    default: SizingBuckets
    per_channel: dict[str, SizingBuckets] = Field(default_factory=dict)
```

Loaded by `load_policy` at startup. Invalid values (e.g., `shares: 2.0`) fail validation immediately, preventing pathological sizing.

## Testing

### Unit tests

- `TraderClassifier`:
  - LLM bucket=HIGH, stated_size=3%, conf=0.85 → bucket forced to LOW, size_source=`wse_small_size_override`
  - LLM bucket=HIGH, stated_size=10%, conf=0.85 → bucket stays HIGH (≥7.5%, no override)
  - LLM bucket=HIGH, stated_size=None, conf=0.85 → bucket stays HIGH (no override)
  - Shortcut: stated 5% + single ticker + entry verb → bucket=LOW, no size_pct set (deferred to SizingResolver)
  - Shortcut: stated 10% + single ticker + entry verb → bucket=HIGH, no size_pct set
  - LLM bucket=LOW, stated_size=3%, conf=0.85 → bucket=LOW (no change, no override fired)
- `SizingResolver`:
  - Channel=stock-talk-portfolio, bucket=HIGH → shares_pct=0.20, options_pct=0.05
  - Channel=mystic, bucket=LOW → shares_pct=0.10, options_pct=0.05
  - Channel=urkel, bucket=HIGH → shares_pct=0.10, options_pct=0.05 (default fallback)
  - Channel=unknown_new_trader, bucket=LOW → shares_pct=0.05, options_pct=0.05 (default fallback)
- `OrderSizer`:
  - Equity: `net_liq=100k`, `margin_multiplier=2.0`, `shares_pct=0.10` → allocation=20k
  - Option: `net_liq=100k`, `margin_multiplier=2.0`, `options_pct=0.05` → allocation=10k
- `ReferencePriceCapture`:
  - Successful quote → `ctx["reference_price"]` set
  - Quote raises `IBGatewayUnavailable` → fail with `reference_price_unavailable`
- `OptionsChaseGuard`:
  - `reference=100, current=109` → success (passes through)
  - `reference=100, current=110` → success (boundary; ≤ ref × 1.10)
  - `reference=100, current=111` → skip with `options_chase_skip`
- `OptionsMarketSubmitter`:
  - Persists with `instrument_type='option'` and `parent_intent_id` set to shares intent_id
  - Does NOT insert into `trade_intent_trims` (no exit ladder)
  - `side="short"` → skip (consistent with shares submitter)
- `TradeIntentWriter`:
  - Writes shares row first, options row second; options row has `parent_intent_id == shares.intent_id`
- Existing tests carried over (trim ladder, RthEntryGuard, etc.) from the 2026-05-04 spec.

### Integration test (paper account)

- Synthetic Discord HIGH signal from `stock-talk-portfolio` channel during RTH:
  - Verify two `trade_intents` rows: shares (20% × net_liq × 2.0 of buying power notional) and options (5% × ditto)
  - Verify both fill via MKT
  - Verify `trade_intent_trims` has R1+R2 armed for the shares row only
- Synthetic signal where reference quote drops (price ran +12% before options submit): verify options leg skipped, shares leg + trims unaffected
- Synthetic signal from new trader `urkel`: verify default sizing (10% shares HIGH, 5% LOW) applies
- Channel-ID swap: send a synthetic message under new ID `1229546005788098580` → verify it routes to `stocktalkweekly` profile (not `mystic`)

### Latency check

Add timestamps for `signal_received → reference_quote → shares_ack → shares_filled → options_ack → options_filled`. Verify p95 of `signal_received → shares_ack` stays under 2.5 s (the original 2026-05-04 target). Options leg adds ~1 second; total p95 `signal → both legs filled` target is 5 s.

## Risks & Trade-offs

1. **Per-signal deployment up to 25% of (NetLiq × 2.0).** A HIGH signal from STP commits 25% of (NetLiq × margin_multiplier). On a $100k account at 2× margin, that's $50k notional in one ticker. Less than the prior 2026-05-04 80%-of-BP design, but still meaningful concentration.
   - **Mitigation:** `CooldownGuard` (30 min, per-ticker), `RthEntryGuard` (no premarket fires), and the geometric drawdown of subsequent signals (since `OrderSizer` recomputes the base each signal off live `net_liq`, a position that goes to -50% reduces the next sizing accordingly).
   - No explicit daily-deployed-capital cap. If paper testing shows runaway concentration, add as follow-up spec.

2. **`NetLiquidation × margin_multiplier` is not the same as live `BuyingPower`.** Live `BuyingPower` shrinks as positions are bought (cash → stock); `NetLiq × margin_multiplier` does not, because NetLiq is stable across position transitions (cash + stock value). This is the intended behavior — fixed denominator. But it means we can compute a sizing larger than current `BuyingPower` allows, especially mid-day after several signals. IB will reject the order at the gateway boundary if BP is insufficient. We log the rejection and move on. Same accept-the-race handling as the 2026-05-04 spec.

3. **Two orders per signal = two failure points.** Either leg can fail independently. The design accepts asymmetric outcomes (shares filled, options not) — the chase guard is intentional, the rejection-tolerance is acceptable. Operationally this means the `trade_intents` table will sometimes have orphan shares rows with no matching options row. Not a bug; report it transparently in the telegram digest so the user sees what happened.

4. **Reference price is a single-quote snapshot.** A microbursting tape between snapshot and shares submit could trip the chase guard immediately (if shares fill at +5% in the same second the reference was quoted, options chase guard sees `current ≈ shares_fill ≈ ref + 5%`, passes; if it then runs to +12%, fails). Single-quote snapshot is acceptable for v1. Future spec could use a short-window VWAP or median-of-N quotes.

5. **WSE-fix has a side effect on multi-ticker pairs.** A message like "Added 5% CEG, opening 5% VST" both have stated_size=5% but the message is genuinely two LOW positions. Current behavior: the LLM picks one ticker (whichever it deems primary), shortcut fails on multi-ticker, classifier returns one ticker LOW. This is the same as today — the multi-ticker case is generally degraded. Accepted limitation.

6. **Channel-ID swap rewrites past data labels.** Existing `signal_events` rows with `channel='mystic'` were authored by Stock Talk Weekly; rows with `channel='stock-talk-portfolio'` were authored by mystic. Going forward, new captures will have correct channel labels. Historical rows are NOT retroactively rewritten — they keep their old (wrong) labels. Acceptable: queries that group by channel for analytics are already off historically; the fix is forward-only. The auto-memory entry for channel mappings will be updated.

7. **MAX_STATED_SIZE removal changes shortcut behavior.** Today, "20% pos $XYZ" → shortcut bucket=HIGH but `size_pct` capped at 10%. Under new design, "20% pos $XYZ" → shortcut bucket=HIGH; size comes from per-channel table (e.g., 15% shares for STP HIGH, not 20%). Shortcut is now bucket-only; size never exceeds the per-channel cap. This is the intended behavior — stated percentages are *signal*, not size, mirroring the 2026-05-04 spec's reasoning.

8. **Options held forever (no exit ladder).** If options run +200%, no automated trim. User closes manually or holds to expiry. Acceptable for v1. A follow-up spec could add an options exit ladder (different math: option price ≠ underlying price; multiplier matters; theta affects hold). Out of scope here.

9. **Live trading remains deferred.** Same gates as 2026-05-04 spec — paper account only. Switch to live requires explicit human go-ahead and follow-up spec.

## Migration Notes

Sequence of edits at deploy time:

1. Apply the `parent_intent_id` ALTER to `trade_intents`. Additive, safe.
2. Apply the `trade_intent_trims` table + `fill_qty` ALTER from the 2026-05-04 spec (still needed; this spec doesn't supersede those data-model changes).
3. Update `policy.yaml` per the diff above (channel ID swap, sizing table, margin_multiplier, options_chase_threshold_pct, removal of fallback_to_stock_if_no_options).
4. Add new trader profile YAMLs (`urkel.yaml`, `pup-danny.yaml`).
5. Restart with `agent-stop && agent-start` to load the new policy.

No flag-based rollout. Paper account, atomic switch.

## Success Criteria

1. Signal from `stock-talk-portfolio` (`1229546005788098580`) HIGH → routes to `stocktalkweekly` profile, sizes shares at 20% × NetLiq × 2.0 and options at 5% × NetLiq × 2.0.
2. Signal from `mystic` (`1217309136681832540`) LOW → routes to `mystic` profile, sizes shares at 10%, options at 5%.
3. Signal from `urkel` HIGH → routes to `urkel` profile, sizes shares at 10% (default), options at 5%.
4. Reference price snapshot occurs before shares submit; chase guard re-quotes after shares fill.
5. WSE-style "3% pos" message classified as LOW even when LLM returns HIGH.
6. Two `trade_intents` rows per successful signal (shares + options), linked by `parent_intent_id`.
7. Trim ladder arms on shares only.
8. When `current_price > reference_price × 1.10` between reference and options submit, options leg skipped with `options_chase_skip`. Shares leg unaffected.
9. `MAX_STATED_SIZE` and `fallback_to_stock_if_no_options` removed from code/config.
10. `_assert_paper_guard` remains in force; live trading impossible without code change.
11. Telegram digest reports both legs (or skip reasons) per signal.

## Open Questions

None blocking. Two non-blocking items for follow-up specs (not v1):
- Options exit ladder (when, how, against what price).
- Daily total-deployed-capital cap as a circuit breaker.
