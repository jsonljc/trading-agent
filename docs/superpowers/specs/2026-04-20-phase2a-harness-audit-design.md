# Phase 2a Harness Audit — Design Spec

**Date:** 2026-04-20
**Scope:** Improvements to Phase 2a (before implementation) based on harness/knowledge-layer/instruction-contract audit

---

## Context

Phase 1 is complete. Phase 2a is planned (see `docs/superpowers/plans/2026-04-19-phase2a-signal.md`) but not yet implemented. This audit identified three gaps between the Phase 2a plan and the harness/knowledge-layer/instruction-contract principles:

1. No single behavioral contract file
2. No `MarketHoursGuard` skill despite `market_hours` policy existing
3. No dry-run mode

---

## What Is Not Changing

The Phase 2a plan is otherwise correct. This spec adds three targeted improvements on top of it. All other Phase 2a tasks (DB schema, ParsedTradeSignal model, ParsedSignalStore, ApprovalPolicy, TradeSignalExtractor, ParsedSignalWriter, SignalDispositionResolver, TelegramClient keyboard, SignalApprovalGate, registry, main.py, E2E tests, AXDiscordWatcher) are unchanged.

---

## Addition 1: `AGENT_CONTRACT.md`

**File:** `AGENT_CONTRACT.md` (repo root)

A single human-readable behavioral contract. All LLM skill system prompts cite it as the source of truth. It is not code — it is the authoritative prose definition of what the agent must and must not do.

### Content

```markdown
# Agent Contract

This file defines what the agent must and must not do. It is the behavioral
source of truth across all pipeline stages. Code enforces it; this file names it.

## Outcome semantics

- fail: invariant violated, unexpected/malformed/unsafe state — operator attention may be needed
- skip: valid input but intentionally non-actionable or policy-disallowed — normal pipeline path
- success: stage completed, downstream progression allowed

## Signal intake

- Must resolve signal_type and confidence before advancing past TradeSignalExtractor
- Must resolve a non-null, non-ambiguous ticker before advancing past TickerResolver
- Unresolved ticker → fail with reason; never continue with null ticker
- Ambiguous ticker → fail with reason; never guess
- Never infer ticker from vague company references; only accept high-confidence,
  unambiguous resolver output
- Must resolve conviction_bucket and target_allocation_pct before advancing past ConvictionClassifier
- Unknown or unrecognized signal_type → fail with reason; never pass through
- Missing required context field → fail; never proceed on partial context

## Approval gate

- LONG_SIGNAL and ADD_SIGNAL require human approval via Telegram keyboard
- CLOSE_SIGNAL and PARTIAL_CLOSE auto-approve when auto_approve_closes=true
- Timeout → skip; never auto-approve on timeout
- Rejection → skip
- Approval is an operator visibility surface; a signal may receive an approval
  message even if it cannot execute (e.g., outside market hours)

## Execution eligibility

- Option execution requires RTH (09:30–16:00 ET); fail outside that window
- Equity execution: premarket allowed from 04:00 ET if stock_premarket_allowed=true;
  afterhours queued if stock_afterhours_queue=true; otherwise fail
- MarketHoursGuard runs after approval, before order planning

## Instrument selection

- Prefer call options for LONG_SIGNAL and ADD_SIGNAL when prefer_options=true
- Only consider expiries >= min_expiry_days
- Only select contracts that pass liquidity guards (e.g., min_bid, max_spread_pct, and other configured liquidity thresholds)
- Contract ranking is deterministic; the LLM must never select the final contract directly
- If no option passes filters → fallback to stock when fallback_to_stock_if_no_options=true
- If option budget cannot afford one contract → fallback to stock when
  fallback_to_stock_if_no_options=true
- If stock fallback is disabled and one option contract is unaffordable → skip with reason
- Never force an option because options are preferred when no valid contract exists

## Signal upgrade

- A WATCHLIST_ONLY or NO_ACTION message may be upgraded to LONG_SIGNAL
  (conviction=low, target_allocation_pct=0.05) only when all of:
    - ticker resolved and unambiguous
    - message classified as bullish catalyst/news
    - QQQ > EMA9 and EMA21; SPY > EMA9 and EMA21
- This upgrade is a deterministic rule, not a free-form LLM decision
- Never override CLOSE_SIGNAL, PARTIAL_CLOSE, or bearish language
- Never upgrade an ambiguous or unresolved ticker
- Never size above 5% on this path
- This rule is only active when regime overlay data is available and
  enable_regime_catalyst_upgrade=true; otherwise default remains WATCHLIST_ONLY/NO_ACTION
- RegimeCatalystUpgrader is deferred until market-data and EMA infrastructure exist

## Write actions

- Never submit an order without a persisted ExecutionPlan
- Never submit an order before OrderPolicyGuard passes
- Never submit live orders when paper_trading_only=true
- Never submit a duplicate execution for the same symbol/contract/disposition
  within the active dedupe/cooldown window when policy blocks it
- Never auto-promote from dry_run=true to live

## Dry-run mode

- When dry_run=true: approval messages are logged, not sent; all other
  pipeline behavior is identical
- dry_run=true must never be treated as equivalent to paper_trading_only=true;
  they are separate controls
- When dry_run=true and dry_run_auto_approve=false: approval gate returns skip
  with reason "dry_run: approval suppressed"
- When dry_run=true and dry_run_auto_approve=true: approval gate returns success
  with approval_status=approved_simulated (never indistinguishable from a real approval)
```

### Implementation notes

- No runtime dependency — it is a markdown file
- LLM skill system prompts (`TradeSignalExtractor`, `TickerResolver`, `ConvictionClassifier`) should add a header line citing this file
- No automated enforcement of the contract against code — that is the operator's responsibility during review

---

## Addition 2: `MarketHoursGuard` skill

### Chain position

After `SignalApprovalGate`, before order planning (Phase 2b). The approval message fires first (operator visibility), then execution eligibility is checked.

In Phase 2a (signal-only), `MarketHoursGuard` acts as a true chain blocker: it fails the pipeline and prevents downstream execution tasks from running, while preserving operator visibility via the approval message that already fired. It is not merely an annotation — it is included now so that Phase 2b execution tasks can be inserted after it without chain restructuring.

Updated Phase 2a chain:
```
MessageNormalizer
TradeSignalExtractor
IdempotencyCheck
TickerResolver
ConvictionClassifier
ParsedSignalWriter
SignalDispositionResolver
SignalApprovalGate        ← approval visibility first
MarketHoursGuard          ← execution eligibility gate; fails chain if ineligible
```

### Behavior

Reads from context:
- `asset_type_hint`: `"option"` or `"equity"`
- Current ET time via injected `time_fn` (default: `datetime.now(ZoneInfo("America/New_York"))`)

Logic:
- **Options:** fail if outside RTH (`rth_start`–`rth_end`)
- **Equity premarket:** pass if time >= `stock_premarket_start` and policy `stock_premarket_allowed=true`
- **Equity afterhours:** return success with `updates={"queued": True}` if policy `stock_afterhours_queue=true`
- **Otherwise:** fail with reason prefixed `execution_ineligible:` including current ET time and allowed window (e.g., `execution_ineligible: option outside RTH (current ET 17:00, allowed 09:30–16:00)`)

### Files

| File | Action |
|---|---|
| `skills/risk/market_hours_guard.py` | Create |
| `tests/unit/test_market_hours_guard.py` | Create |
| `agent/registry.py` | Insert `MarketHoursGuard` after `SignalApprovalGate` in `build_phase2a_signal_chain` |

### Test matrix

| Scenario | asset_type_hint | time (ET) | Expected |
|---|---|---|---|
| Options in RTH | option | 10:00 | success |
| Options outside RTH | option | 17:00 | fail |
| Options at open boundary | option | 09:30 | success |
| Options before open | option | 09:29 | fail |
| Equity premarket allowed | equity | 06:00 | success |
| Equity premarket before window | equity | 03:59 | fail |
| Equity afterhours queue | equity | 17:00 | success, queued=True |
| Equity afterhours no queue | equity | 17:00 (queue=false) | fail |

---

## Addition 3: Dry-run policy flag

### New policy model

Add a `HarnessPolicy` model to `agent/policy.py`:

```python
class HarnessPolicy(BaseModel):
    dry_run: bool = False
    dry_run_auto_approve: bool = False
```

Add to `PolicyModel`:
```python
harness: HarnessPolicy = HarnessPolicy()
```

Add to `config/policy.yaml`:
```yaml
harness:
  dry_run: false
  dry_run_auto_approve: false
```

### Behavior matrix

| `dry_run` | `dry_run_auto_approve` | Telegram | `approval_status` | Pipeline continues |
|---|---|---|---|---|
| false | — | sent | `approved` / `rejected` / `timeout` | per outcome |
| true | false | logged | — | no (`skip`) |
| true | true | logged | `approved_simulated` | yes |

### Changes to `SignalApprovalGate`

```python
if self._policy.harness.dry_run:
    msg = _format_approval_message(ctx)
    logger.info("DRY RUN approval suppressed:\n%s", msg)
    if self._policy.harness.dry_run_auto_approve:
        await self._store.update_approval(signal_id, "approved_simulated", _now(), None)
        return SkillResult(status="success", updates={"approval_status": "approved_simulated"})
    return SkillResult(status="skip", reason="dry_run: approval suppressed")
```

### Changes to `main.py` `on_fail` and `on_skip`

Both Telegram side-effect callbacks are guarded by `dry_run`:

```python
async def on_fail(ctx: Context, reason: str) -> None:
    if policy.harness.dry_run:
        logger.info("DRY RUN error digest suppressed: %s", reason)
        return
    # ... send telegram message

async def on_skip(ctx: Context, reason: str) -> None:
    if policy.harness.dry_run:
        logger.info("DRY RUN skip digest suppressed: %s", reason)
        return
    # ... send telegram message (if skip digests are enabled)
```

### Files

| File | Action |
|---|---|
| `agent/policy.py` | Add `HarnessPolicy`, add `harness` field to `PolicyModel` |
| `config/policy.yaml` | Add `harness` section |
| `skills/rollout/signal_approval_gate.py` | Honor `dry_run` / `dry_run_auto_approve` |
| `main.py` | Guard `on_fail` telegram send behind `dry_run` check |
| `tests/unit/test_signal_approval_gate.py` | Add dry-run test cases |

---

## Summary of Phase 2a plan additions

Three new tasks to append to `docs/superpowers/plans/2026-04-19-phase2a-signal.md`:

| Task | Description | Prerequisite |
|---|---|---|
| Task 14 | Create `AGENT_CONTRACT.md` | None — do first |
| Task 15 | `HarnessPolicy` + dry-run flag | Task 4 (policy changes) |
| Task 16 | `MarketHoursGuard` skill | Task 10 (registry) |

Suggested order within Phase 2a: Task 14 first (no dependencies), Task 15 alongside Task 4, Task 16 alongside Task 10.

---

## Out of scope

- `RegimeCatalystUpgrader`: deferred until market-data and EMA infrastructure exist
- Live knowledge layer (positions, buying power): Phase 2b
- `_safe_json` refactor: low priority, no safety impact
- Shared Anthropic client: low priority, no safety impact
