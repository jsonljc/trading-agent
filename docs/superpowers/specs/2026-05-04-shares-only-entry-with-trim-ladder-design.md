# Shares-Only Entry with Trim Ladder — Design

**Status:** Draft (revision 2 — audit fixes applied, sizing/trim ladder updated)
**Author:** Jason (with Claude)
**Date:** 2026-05-04

## Problem

Current entry path defaults to long-dated calls walked as limit orders for up to ~10 s
(`skills/execution/price_walker.py:80`). Two problems for time-sensitive Discord signals:

1. **No guarantee of fill.** If the limit walk exhausts, the position is missed entirely.
2. **Latency.** Option chain lookup + LMT walk can push first ACK 1–10+ s after the Discord
   message — sometimes longer than the trader's edge.

## Goal

Replace the entry path with a **shares-only, market-order** entry sized by conviction tier,
plus an automatic upside trim ladder. Optimize for fast, certain fills at the cost of
giving up option leverage.

Out of scope (explicitly deferred): options entry, partial-fill recovery beyond what
IB returns natively, exit-ladder anti-whipsaw logic, live-trading switch (paper only
for v1 — see "Live trading deferred" below). **Stop-loss is not deferred — it is
explicitly excluded by design**: the long-tail 20% of the position is intended to
"hold forever," so a downside stop is incompatible with the strategy.

## Behavior

### Entry

| Conviction | Allocation | Order type | Instrument |
|---|---|---|---|
| HIGH | 80% of buying power | MKT | Underlying shares (STK) |
| LOW  | 40% of buying power | MKT | Underlying shares (STK) |
| SKIP | — | — | — |

Quantity = `floor(buying_power * size_pct / last_quote)`. If quantity < 1, fail with
`insufficient_buying_power` (existing OrderSizer behavior — no change).

### Exit (Trim Ladder)

After the entry fill, two upward-only trim rungs arm. Each fires at most once:

| Rung | Trigger | Action |
|---|---|---|
| R1 | last price ≥ avg_fill_price × 1.05 | SELL 40% of *original filled* qty, MKT |
| R2 | last price ≥ avg_fill_price × 1.10 | SELL 40% of *original filled* qty, MKT |

Cumulative: by the time R2 fires, **80% of the position is sold and the remaining
20% holds forever** (no further trim, no stop). Both trim percentages are computed
against the **original filled qty**, not the remaining qty, so R1+R2 = 80% even
if R1 fired earlier.

Rounding: `round_half_up(original_qty * pct)`, minimum 1 share. If a rung's
computed trim qty would exceed remaining qty, trim what's left and mark the rung fired.

No re-arming after a price drops back below threshold. Rungs fire once and stay fired.

The "hold forever" 20% tail is the strategic point of this design: take 80% of the
gain off the table quickly via trims, ride the remaining 20% for the long thesis.

### Exit monitoring

Single background poll loop running during RTH:

- Polls every **2 s** (configurable: `execution.exit_poll_interval_seconds`)
- For each open position with un-fired trim rungs, calls `gateway.get_quote(ticker)`
- Fires R1/R2 inline when threshold crossed
- On each rung fire, persists the new state before placing the order

Outside RTH the loop sleeps. Trim rungs do NOT fire pre-market or after-hours
(stocks can gap, and we have no stop-loss). The first eligible RTH tick at
or above threshold fires the rung.

## Architecture

### What changes

**Modified:**
- `skills/signal/trader_classifier.py` —
  - Set `SIZE_LOW = 0.40`, `SIZE_HIGH = 0.80`, `MAX_STATED_SIZE = 0.80`.
  - Set `SIZE_HIGH_SHORTCUT_THRESHOLD = 0.60` (message-stated sizes ≥60% map to
    HIGH bucket; below to LOW).
  - **Audit fix #3 — stated-size shortcut path:** today the shortcut path
    (`trader_classifier.py:74`) sets `size_pct = stated_size_pct / 100.0`. Under
    the new design, `size_pct` must always be exactly `SIZE_LOW` or `SIZE_HIGH`.
    Change the shortcut so stated_size_pct only chooses the bucket; the actual
    `size_pct` is then `SIZE_HIGH if bucket == "HIGH" else SIZE_LOW`. Stated
    percentages are *signal*, not *size*.
- `skills/execution/trade_intent_writer.py` — **audit fix #1:** line 45 hardcodes
  `"instrument_type": "option"`. Change to `"equity"` (no other path now).
- `agent/registry.py:build_phase2b_execution_chain` — rebuild as shares-only chain.
  Drop `ChainLookup`, `InstrumentMarketabilityGuard`, `ContractSelector`, `OrderPricer`,
  `PriceWalker` from the entry path. Add `RthEntryGuard`, `EquityContractBuilder`,
  and `SharesMarketSubmitter` (all new — see below).
- `infra/ib/gateway.py:place_order` — branch on `PreparedOrder.order_type`. When
  `order_type == "MKT"`, build `MarketOrder(...)` instead of `LimitOrder(...)`.
  This is the same gateway method the exit ladder calls directly for trim sells
  (see "Trim sell submission path" below).
- `skills/execution/order_sizer.py` — equity branch already exists (lines 42–49). Verify
  it works with the new larger size_pct values; no logic change expected. Note
  there is a redundant `qualify()` here vs. `EquityContractBuilder` — accept the
  duplicate round-trip for v1 (cleanup is YAGNI for now).
- `infra/ib/models.py:PreparedOrder` — make `limit_price: float | None` (None for MKT).

**New:**
- `skills/execution/rth_entry_guard.py` — **audit fix #2:** Reads
  `execution_session` set by `ExecutionEligibilityGuard`. If session != "rth",
  return `SkillResult(status="skip", reason="entry_outside_rth")`. Premarket and
  afterhours signals are dropped (not queued — queueing pre-market signals to
  fire at 09:30 has its own design questions, deferred). Insert this guard
  between `ExecutionEligibilityGuard` and `EquityContractBuilder`.
- `skills/execution/equity_contract_builder.py` — replaces ContractSelector for the
  shares path. Builds a `BrokerContractRef(sec_type="STK", symbol=ticker, exchange="SMART")`
  and qualifies it. Single round trip.
- `skills/execution/shares_market_submitter.py` — replaces PriceWalker for the shares
  path. Submits one MKT order via `gateway.place_order(contract, PreparedOrder(order_type="MKT", limit_price=None, ...))`,
  waits for fill (uses existing `gateway.wait_fill`), records `fill_price` and
  `fill_qty` to `trade_intents`, then arms trim rungs by inserting into the new
  `trade_intent_trims` table (see schema below).
- `agent/exit_ladder.py` — background asyncio task. Started by `agent/orchestrator.py`
  alongside the existing reconciler. Polls open positions with armed rungs, fires trims,
  updates state. **Audit fix #4 — trim sell mechanism:** the ladder calls
  `gateway.place_order(...)` directly with `PreparedOrder(action="SELL", order_type="MKT", limit_price=None, ...)`.
  It does NOT route through `OrderSubmitter` (which is hardcoded LMT and tied to
  the executions table — wrong abstraction for a sell-side trim). After the sell
  trade returns, it calls `gateway.wait_fill` to record actuals into
  `trade_intent_trims`.

**Unchanged but bypassed:**
- `skills/execution/chain_lookup.py`, `instrument_marketability_guard.py`,
  `contract_selector.py`, `order_pricer.py`, `price_walker.py` — still in repo,
  not in entry chain. They're not deleted only to keep the diff focused; they
  can be removed in a follow-up cleanup. **No "fall back to options" path
  exists in the new design** — we are stocks-only, period.

### SHORT signals — audit fix #5

`TraderClassifier` may produce `side="short"`. The trim ladder is upward-only
and has no inverse logic. **For v1, short signals are dropped at the
`SharesMarketSubmitter` boundary** with `SkillResult(status="skip", reason="unsupported_short_signal")`.
A separate spec will handle shorts if/when needed (mirror ladder, borrow availability,
locate fees, etc. — non-trivial).

### Component boundaries

```
TraderClassifier  →  size_pct ∈ {0.40, 0.80}; side dropped if "short"
       ↓
TradeIntentWriter  →  one row in trade_intents (instrument_type="equity")
       ↓
ChannelPolicyGuard, CooldownGuard, ExecutionEligibilityGuard  (unchanged)
       ↓
RthEntryGuard  →  skip if execution_session != "rth"
       ↓
EquityContractBuilder  →  qualified STK contract
       ↓
OrderSizer  →  qty from buying_power × size_pct ÷ last_quote
       ↓
SharesMarketSubmitter  →  short-side check → MKT BUY → wait fill → persist
                          fill_qty/fill_price → INSERT R1+R2 rows into
                          trade_intent_trims (armed)
       ↓
[exit ladder runs in background, polls quotes, fires trims at +5% / +10%]
```

The exit ladder is fully decoupled from the entry chain — it reads from the
`trade_intent_trims` table (new) and the position record. It does not share
in-memory state with the entry pipeline. This is what makes restart-recovery clean.

## Data Model Changes

New table for trim-ladder state:

```sql
CREATE TABLE IF NOT EXISTS trade_intent_trims (
    intent_id            TEXT NOT NULL,
    rung                 INTEGER NOT NULL,         -- 1 or 2
    threshold_pct        REAL NOT NULL,            -- 0.05, 0.10
    trim_pct             REAL NOT NULL,            -- 0.40, 0.40
    armed_at             TEXT NOT NULL,            -- after entry fill
    fired_at             TEXT,                     -- null until rung fires
    fire_price           REAL,                     -- quote that triggered
    sold_qty             INTEGER,                  -- actual qty sold
    sold_avg_price       REAL,                     -- IB avg fill on the sell
    broker_order_ref     TEXT,                     -- the sell order id
    PRIMARY KEY (intent_id, rung),
    FOREIGN KEY (intent_id) REFERENCES trade_intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_intent_trims_unfired
    ON trade_intent_trims(intent_id) WHERE fired_at IS NULL;
```

`trade_intents` needs one additive column:

```sql
ALTER TABLE trade_intents ADD COLUMN fill_qty INTEGER;
```

`fill_price` already exists; `fill_qty` does not. The trim ladder needs original
filled qty to compute trim sizes (R1+R2 always = 80% of *original*; remaining
20% holds forever). Per-rung state lives in the new `trade_intent_trims` table
to keep the join simple and avoid widening an already-wide row.

## Behavior Details

### Idempotency

- Entry submission idempotency key: existing pattern,
  `f"{ctx.trace_id}:SharesMarketSubmitter:{ctx.event_id}"`.
- Trim submission idempotency key:
  `f"{intent_id}:trim:R{rung}"`. Stored in `idempotency_keys` table on insert.
  Duplicate fire attempts are rejected at the gateway boundary.

### Restart recovery

On agent startup, `agent/exit_ladder.py` queries:
```sql
SELECT t.*, ti.ticker, ti.fill_price, ti.fill_qty
FROM trade_intent_trims t
JOIN trade_intents ti ON t.intent_id = ti.intent_id
WHERE t.fired_at IS NULL AND ti.execution_state = 'filled';
```
…and resumes polling for those positions. Crashes between
`fire_price` write and broker submission are recovered by the idempotency key —
the next poll tick will retry, and the gateway dedupes.

### Failure modes

| Failure | Behavior |
|---|---|
| MKT entry rejected (no liquidity, halted) | Mark intent `failed`, do NOT arm trims. |
| MKT entry partial fill | Arm trims based on the actually-filled qty. |
| IB Gateway down during exit poll | Poll loop logs and retries next interval. Trims do not fire until reconnected. Acceptable — alternative would be queueing trims, which adds complexity. |
| Trim sell rejected | Log, mark rung `fired_at` with `error` reason, do NOT retry that rung. We never want a retry storm on the sell side; manual intervention. |
| Stock halts mid-position | Quote `get_quote` raises or returns stale; loop skips ticker. Trims fire when trading resumes and quote crosses threshold. |
| Outside-RTH price runup | Trims do not fire. First in-RTH quote at/above threshold fires R1 and possibly R2 in the same tick. |

### Concurrency

The exit ladder loop is single-task, processes positions sequentially. Per-tick wall
time = `n_open_positions × (one quote round-trip)`. With ≤10 open positions and ~30 ms
per quote, well within the 2 s tick. If we ever exceed 10 active positions we'll
parallelize quotes — not now (YAGNI).

### Configuration

`config/policy.yaml` additions:

```yaml
execution:
  ...existing keys...
  exit_poll_interval_seconds: 2
  trim_ladder:
    rungs:
      - threshold_pct: 0.05
        trim_pct: 0.40
      - threshold_pct: 0.10
        trim_pct: 0.40
```

The rung list is a list specifically so we can A/B different ladders later without
schema changes. Code reads it once at startup.

After both rungs fire, 80% of original qty is sold and 20% holds — the "hold
forever" tail. No further configuration controls the long-tail; it's whatever
remains after the configured trims execute.

## Testing

### Unit tests
- `TraderClassifier`:
  - LLM bucket=HIGH, conf 0.85 → size_pct = 0.80
  - LLM bucket=LOW, conf 0.85 → size_pct = 0.40
  - LLM bucket=HIGH, conf 0.65 → downgraded to LOW, size_pct = 0.40
  - Shortcut: stated 50% → bucket=LOW, size_pct = 0.40 (NOT 0.50)
  - Shortcut: stated 70% → bucket=HIGH, size_pct = 0.80 (NOT 0.70)
  - Shortcut: stated 5% → bucket=LOW, size_pct = 0.40
  - side="short" → still classified, but downstream submitter drops it
- `RthEntryGuard`: rth → success; premarket/afterhours → skip.
- `TradeIntentWriter`: writes `instrument_type="equity"` (no longer "option").
- `EquityContractBuilder`: returns qualified STK contract.
- `SharesMarketSubmitter`:
  - Long signal: places MKT, persists fill, inserts both rungs (R1 trim_pct=0.40,
    R2 trim_pct=0.40) into `trade_intent_trims` armed.
  - Short signal: returns skip with `unsupported_short_signal`, no order placed.
- `gateway.place_order`: `order_type="LMT"` → LimitOrder; `order_type="MKT"` →
  MarketOrder (test using a mock IB).
- Exit ladder: given a fake gateway and a fixed quote sequence, confirm:
  - +5% crosses → R1 fires, sells 40% of original (rounded), MKT
  - +10% crosses → R2 fires, sells another 40% of original, MKT
  - After both rungs fire: 20% of original qty remains, no further sells
  - Round-trip below threshold then above → fires once only
  - R1+R2 in same tick (gap up >10%) → both fire in order in the same poll iteration
  - Quote below thresholds → no fires
  - Trim qty rounding: original_qty=11 → R1 sells round(11*0.40)=4, R2 sells 4,
    leaves 3 (≈27%, acceptable for tiny positions)

### Integration test (paper account)
- Send a synthetic Discord signal, verify entry MKT places and fills.
- Manually move a small-cap stock or use a test ticker to verify the trim
  threshold crosses — or stub the quote source to a fake stream.
- Restart the agent mid-position with un-fired rungs; confirm rungs survive
  and fire correctly after restart.

### Latency check
Add timestamps on `received_at`, `entry_ack_at`, `entry_filled_at` for the new
path. Verify Discord-message → entry-ACK is consistently <2.5 s on the LLM
path and <500 ms on the shortcut path. (Adding this is part of the work, not
deferred — the whole point of this redesign is faster fills.)

## Risks & Trade-offs

1. **Aggregate exposure jumps from 5–10% to 40–80% of BP per signal.**
   - A single HIGH signal commits 80% of buying power to one stock. With no
     stop-loss (excluded by design — see Goal section), a -50% drawdown on that
     one ticker = -40% of account.
   - **Mitigation:** `CooldownGuard` (30 min, per-ticker) limits re-entries on
     the same ticker. `ExecutionEligibilityGuard` enforces market-hours.
     Concurrent BP race (below) self-limits.
   - **No daily-deployed-capital cap in v1** — judged unnecessary because
     OrderSizer reads live `buying_power` per signal, so each successive
     signal naturally sizes off whatever's left. After one HIGH signal at 80%
     BP, the next signal sizes off ~20% remaining → 16% allocation. After the
     second, off ~4% → 3.2%. Geometric drawdown protection. If this proves
     too aggressive in paper testing, add a daily cap as a follow-up spec.

2. **Concurrent BP race — accepted behavior.** If two HIGH signals arrive
   within milliseconds, both `OrderSizer` calls may read the same pre-trade
   `buying_power` and each compute 80% allocation. The first MKT order goes
   through; the second is **rejected by IB for insufficient funds**. We log
   the rejection and move on. This is acceptable: the alternative (a global
   sizing lock) adds complexity for a rare race that already self-resolves
   safely.

3. **No leverage.** Shares ≠ deep ITM calls in dollar P/L. Spec choice — speed
   and certainty over leverage. Not a bug.

4. **MKT in pre-market is blocked by `RthEntryGuard`.** Pre-market signals
   are dropped, not queued — see audit fix #2. If a trader posts a great
   signal at 6 AM, we miss it. Acceptable for v1; queueing is its own design
   problem (does the signal still apply at 09:30? what if the price gapped?).

5. **No partial-fill walking.** If IB fills only part of the MKT (rare for
   liquid names, possible for thin tickers), we accept the partial and arm
   trims for the partial qty. We do NOT chase the unfilled remainder.

6. **Quote source for trim trigger.** `gateway.get_quote()`
   (`infra/ib/gateway.py:281-303`) returns the first valid of `ask → last → close`.
   For an upward trim trigger, ask-first means we fire slightly earlier than a
   true print (someone *offering* at the threshold, not necessarily a trade).
   Accepted — the difference is at most one tick on liquid names, and the bias
   is in the safer direction (lock in gain a hair early rather than miss it).

7. **Hold-forever 20% accumulates indefinitely.** Each HIGH signal that runs
   to +10% leaves 20% of the original 80% BP allocation = 16% of pre-trade BP
   tied up forever in that ticker. After ~5 such signals, ~80% of original
   account capital is locked in long-tail positions. **This is the strategic
   intent** — the user wants long-tail exposure to high-conviction bets — but
   it does mean the agent's *active* trading capital shrinks over time. No
   mitigation; this is the design.

8. **Live trading deferred.** v1 stays on paper (`paper_account_prefixes: ["DU"]`,
   `_assert_paper_guard()` in `infra/ib/gateway.py:414` remains in force).
   Switch to live is a follow-up spec gated on:
   (a) ≥1 week clean paper run with ≥5 real trim ladders firing as expected,
   (b) zero orphaned trim state across at least one agent restart,
   (c) p95 entry latency confirmed under 2.5 s,
   (d) explicit human go-ahead. The follow-up spec should also propose a hard
   total-deployed-BP cap for the first live week as a circuit breaker.

## Open Questions

None blocking. All deferred items listed in "Out of scope" above.

## Migration Notes

This is a behavior change, not a schema migration in the destructive sense:
- Add the new `trade_intent_trims` table (additive, safe).
- Existing in-flight option positions — none expected at deploy time
  (paper account, manual cleanup acceptable).
- Roll out by changing the chain registration. No flag — the option chain
  isn't being used today in a way that benefits from gradual rollout.

## Success Criteria

1. Discord HIGH signal during RTH → MKT entry placed and filled within 2.5 s, p95.
2. Discord HIGH signal sized at exactly 80% of buying_power (LOW = 40%).
3. After fill, two trim rungs visible in `trade_intent_trims` with
   `fired_at = NULL`, both `trim_pct = 0.40`.
4. Synthetic +5% price move triggers R1 within 2 s of the quote crossing,
   sells 40% of original qty MKT.
5. Synthetic +10% price move triggers R2 within 2 s of the quote crossing,
   sells another 40% of original qty MKT.
6. After both rungs fire: 20% of original qty remains in account, no further
   trim activity, no stop-loss order present.
7. Restart mid-position preserves un-fired rungs and resumes monitoring.
8. Pre-market signal → dropped with `entry_outside_rth`, no order placed.
9. Short signal → dropped with `unsupported_short_signal`, no order placed.
10. No option-related skills appear in the entry execution chain logs.
11. `_assert_paper_guard` remains in force; live trading is impossible without
    code change.
