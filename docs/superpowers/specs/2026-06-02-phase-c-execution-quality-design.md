# Phase C — Execution Quality: Design & Plan

**Goal:** Replace naked MKT entries with marketable-limit orders (slippage-capped),
add a real liquidity gate (MID spread + OI/volume) to the options leg, and handle
partial fills instead of treating anything `!= FILLED` as a hard fail. This changes
*how* entries fill; it does **not** change trade philosophy (no stops, no kill-switch,
downside = follow the trader's explicit sells — out of scope here).

**Branch:** `fix/no-trades-may-8`. One commit per task. TDD throughout.

**Decisions locked with the operator (2026-06-02):**
- Limit model: `limit = live_ask × (1 + cap)` (marketable; fills at NBBO, caps chase).
- Separate caps per instrument: options 5%, shares 1%.
- Liquidity gate fails **open** when OI/volume are unavailable (delayed data returns
  none) — gate on spread alone, log the gap. Spread ceiling 0.10 of **mid**.
- Partial shares fill → record fill, arm trims on filled qty, cancel residual, and
  **continue** to the options leg (a partial is still a fill).

---

## Stream 1 — Marketable-limit pricing

**New policy fields** (`ExecutionPolicy`, `config/policy.yaml`):
`options_slippage_cap_pct: 0.05`, `shares_slippage_cap_pct: 0.01`.

**Helper** `skills/execution/_pricing.py`:
```python
import math
def marketable_limit(ask: float, cap_pct: float) -> float:
    """Round-up-to-the-penny marketable BUY limit: ask * (1+cap), >= ask."""
    return math.ceil(ask * (1.0 + cap_pct) * 100) / 100
```

**Shares submitter:** before submit, fetch the live equity ask via
`gateway.get_quote(ticker)`; `limit = marketable_limit(ask, shares_cap)`; submit a
`PreparedOrder(order_type="LMT", limit_price=limit, tif="DAY")`. A `get_quote`
failure returns `fail` (`broker_unavailable`), same shape as a place_order failure.

**Options submitter:** re-fetch the **live** ask via
`gateway.get_option_ask(selected_contract)` (sizing used the cached chain-lookup
ask). `limit = marketable_limit(ask, options_cap)`. Fallback chain: live ask → the
sizing ask (the options `OrderSizer` now stashes `option_ask` in ctx) → if neither
is > 0, fail/partial the leg (can't price a limit). `place_order`'s existing `LMT`
branch is reached unchanged.

*Known limitations:* (1) limits round to the penny (correct for penny-pilot
options); a non-penny-increment contract could draw an IB price rejection —
handled by Phase D rejection handling. (2) The shares limit uses
`get_quote` (ask → last → close); if the ask is unavailable and it falls back to
`last`/`close` below the true ask, the limit may be sub-marketable and not fill —
but that degrades to a *non-fill* (residual cancelled, leg fails), never a bad
fill, so it is acceptable. Adaptive algo intentionally deferred.

## Stream 2 — Liquidity gate (MID spread + OI/volume, fail-open)

- **`get_chain` fix:** `spread_pct = (ask - bid) / mid if mid > 0 else 1.0` (was
  `/ ask`). Populate `open_interest` and `volume` from the ticker when present
  (`callOpenInterest`, `volume`; nan/0 → `None`).
- **Policy `PricingGuards`:** `max_spread_pct: 0.40 → 0.10`; add
  `min_open_interest: int = 100` (active) and `min_volume: int = 0` (available, off
  until live data lands).
- **`ContractSelector`:** after the existing spread gate, reject a candidate only
  when `open_interest`/`volume` are **present and below** threshold; when `None`,
  allow and log "liquidity data unavailable, gating on spread only" (fail-open).

## Stream 3 — Partial-fill handling

`wait_fill` emits `TIMED_OUT_PENDING` with `filled_qty > 0` on a partial. Both
submitters key on `filled_qty > 0`:

- **Shares:** `FILLED` → unchanged. `filled_qty > 0 & not FILLED` → best-effort
  `cancel_order(trade)`, `update_fill(filled_qty, avg, execution_state='filled')`,
  arm trims on the filled qty, return **success**. `filled_qty == 0` → cancel
  residual if pending, return `fail`.
- **Options:** `FILLED` → unchanged. `filled_qty > 0 & not FILLED` → cancel
  residual, `write(...)` with the real `fill_qty`, return success. `filled_qty == 0`
  → cancel residual, `partial_or(..., "fail")`.

`cancel_order` already takes the `trade` object the submitter holds; wrap
best-effort (log on failure — never mask a real fill). Trims arm on the real filled
qty, and the exit ladder requires `execution_state='filled'`, which a partial sets.

## Test surface (TDD, failing-first)
- Shares LMT price = `ceil(ask×1.01)`; options LMT = `ceil(live_ask×1.05)`; options
  cached-ask fallback when live ask ≤ 0.
- `get_chain` spread denominator = mid; OI/volume populated when present, None when
  nan/0.
- `ContractSelector`: present-and-low OI/volume rejects; None allows.
- Partial shares fill → trims armed on filled qty + residual cancelled + success.
- Partial options fill → real qty recorded + residual cancelled.
- Zero-fill timeout → residual cancelled + fail.
- `test_policy_yaml_loads` covers the new fields.
- Existing submitter tests that assert `order_type == "MKT"` are updated to `LMT`.
