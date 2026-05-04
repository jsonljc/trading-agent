# Shares-Only Entry with Trim Ladder — Design

**Status:** Draft
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

Out of scope (explicitly deferred): stop-loss, options entry, partial-fill recovery beyond
what IB returns natively, exit-ladder anti-whipsaw logic.

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
| R1 | last price ≥ avg_fill_price × 1.05 | SELL 10% of *original filled* qty, MKT |
| R2 | last price ≥ avg_fill_price × 1.10 | SELL 20% of *original filled* qty, MKT |

Cumulative: by the time R2 fires, 30% of the position is sold and 70% rides.
Both trim percentages are computed against the **original filled qty**, not the
remaining qty, so R1+R2 = 30% even if R1 fired earlier.

Rounding: `round_half_up(original_qty * pct)`, minimum 1 share. If a rung's
computed trim qty would exceed remaining qty, trim what's left and mark the rung fired.

No re-arming after a price drops back below threshold. Rungs fire once and stay fired.

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
- `skills/signal/trader_classifier.py` — `SIZE_LOW = 0.40`, `SIZE_HIGH = 0.80`,
  `MAX_STATED_SIZE = 0.80`. Update `SIZE_HIGH_SHORTCUT_THRESHOLD` proportionally
  (e.g., 0.60 — message-stated sizes ≥60% map to HIGH bucket; below to LOW).
- `agent/registry.py:build_phase2b_execution_chain` — rebuild as shares-only chain.
  Drop `ChainLookup`, `InstrumentMarketabilityGuard`, `ContractSelector`, `OrderPricer`,
  `PriceWalker` from the entry path. Add `EquityContractBuilder` and
  `SharesMarketSubmitter` (new — see below).
- `infra/ib/gateway.py:place_order` — branch on `PreparedOrder.order_type`. When
  `order_type == "MKT"`, build `MarketOrder(...)` instead of `LimitOrder(...)`.
- `skills/execution/order_sizer.py` — equity branch already exists (lines 42–49). Verify
  it works with the new larger size_pct values; no logic change expected.
- `infra/ib/models.py:PreparedOrder` — make `limit_price: float | None` (None for MKT).

**New:**
- `skills/execution/equity_contract_builder.py` — replaces ContractSelector for the
  shares path. Builds a `BrokerContractRef(sec_type="STK", symbol=ticker, exchange="SMART")`
  and qualifies it. Single round trip.
- `skills/execution/shares_market_submitter.py` — replaces PriceWalker for the shares
  path. Submits one MKT order, waits for fill (uses existing `gateway.wait_fill`),
  records `fill_price` and `fill_qty` to `trade_intents`, then arms trim rungs
  by writing them to a new state row (see schema below).
- `agent/exit_ladder.py` — background asyncio task. Started by `agent/orchestrator.py`
  alongside the existing reconciler. Polls open positions with armed rungs, fires trims,
  updates state.

**Unchanged but bypassed (kept for option-fallback experiments later):**
- `skills/execution/chain_lookup.py`, `instrument_marketability_guard.py`,
  `contract_selector.py`, `order_pricer.py`, `price_walker.py` — still in repo,
  not in entry chain.

### Component boundaries

```
TraderClassifier  →  size_pct ∈ {0.40, 0.80}
       ↓
TradeIntentWriter  →  one row in trade_intents (instrument_type="equity")
       ↓
ChannelPolicyGuard, CooldownGuard, ExecutionEligibilityGuard  (unchanged)
       ↓
EquityContractBuilder  →  qualified STK contract
       ↓
OrderSizer  →  qty from buying_power × size_pct ÷ last_quote
       ↓
SharesMarketSubmitter  →  one MKT order, wait fill, persist fill, arm trim rungs
       ↓
[exit ladder runs in background]
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
    trim_pct             REAL NOT NULL,            -- 0.10, 0.20
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
filled qty to compute trim sizes (R1+R2 always = 30% of *original*). Per-rung
state lives in the new `trade_intent_trims` table to keep the join simple and
avoid widening an already-wide row.

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
        trim_pct: 0.10
      - threshold_pct: 0.10
        trim_pct: 0.20
```

The rung list is a list specifically so we can A/B different ladders later without
schema changes. Code reads it once at startup.

## Testing

### Unit tests
- `TraderClassifier`: HIGH → 0.80, LOW → 0.40, stated size 0.50 → LOW, stated 0.70 → HIGH.
- `EquityContractBuilder`: returns qualified STK contract.
- `SharesMarketSubmitter`: places MKT, persists fill, inserts both rungs into
  `trade_intent_trims` armed.
- Exit ladder: given a fake gateway and a fixed quote sequence, confirm:
  - +5% crosses → R1 fires, sells correct qty
  - +10% crosses → R2 fires, sells correct qty
  - Round-trip below threshold then above → fires once only
  - R1+R2 in same tick (gap up) → both fire in order
  - Quote below thresholds → no fires

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

1. **Aggregate exposure jumps from 5–10% to 40–80% of BP per signal.** A bad
   day with 3 HIGH signals theoretically maxes the account. We have no
   stop-loss (deferred). Mitigation: existing `CooldownGuard` (30 min) and
   `ExecutionEligibilityGuard` still apply; consider a daily-deployed-capital
   cap as a follow-up.
2. **No leverage.** Shares ≠ deep ITM calls in dollar P/L. The user accepts
   this — speed/certainty over leverage. Spec choice, not bug.
3. **MKT in pre-market.** Existing `market_hours` policy allows stock pre-market
   (`stock_premarket_allowed: true`). Pre-market liquidity is thin; MKT in
   pre-market can fill far from mid. **Decision:** entry MKT runs only during RTH
   for v1; signals received pre-market queue (existing `stock_afterhours_queue`
   logic — verify and reuse). Revisit pre-market MKT after we have RTH data.
4. **No partial-fill walking.** If IB fills only part of the MKT (rare for
   liquid names, possible for thin tickers), we accept the partial and arm
   trims for the partial qty. We do NOT chase the unfilled remainder. This is
   simpler and matches the "ensure something fills" goal.
5. **Quote source for trim trigger.** `gateway.get_quote()`
   (`infra/ib/gateway.py:281-303`) currently returns the first valid of
   `ask → last → close`. For an upward trim trigger, ask-first means we fire
   slightly earlier than a true print (someone *offering* at the threshold,
   not necessarily a trade). For v1 we accept this — the difference is at
   most one tick on liquid names, and the bias is in the safer direction
   (lock in gain a hair early rather than miss it). If false-fire data
   suggests otherwise we'll add a `get_last_price()` method that strictly
   returns last/close. Tracked, not blocking.

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

1. Discord HIGH signal → MKT entry placed and filled within 2.5 s, p95.
2. After fill, both trim rungs visible in `trade_intent_trims` with
   `fired_at = NULL`.
3. Synthetic +5% price move triggers R1 within 2 s of the quote crossing.
4. Synthetic +10% price move triggers R2 within 2 s of the quote crossing.
5. Restart mid-position preserves un-fired rungs and resumes monitoring.
6. No option-related skills appear in the entry execution chain logs.
