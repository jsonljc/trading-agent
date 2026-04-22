# IB Gateway Execution Layer — Design Spec

**Date:** 2026-04-22
**Scope:** Phase 2b — full execution layer via IB Gateway paper trading API
**Prerequisite:** Phase 2a signal pipeline complete (ParsedTradeSignal, SignalApprovalGate, MarketHoursGuard)

---

## Context

Phase 2a delivers a complete signal classification and approval pipeline, ending at `MarketHoursGuard`. This spec defines Phase 2b: the execution layer that translates an approved, eligible signal into a real paper trade via IB Gateway.

The architecture principle throughout: **thin harness, fat skills.** `IBGateway` is a transport adapter — it owns broker mechanics, connection lifecycle, and circuit breaking. All business logic (instrument selection, sizing, pricing, policy enforcement) lives in execution skills.

---

## Architecture Principle: Governed Pipeline Orchestrator

The execution layer follows the same Governed Pipeline Orchestrator form as the signal pipeline:

- Each skill has one defined input artifact and one defined output artifact
- Skills own policy; the gateway owns broker mechanics
- The approval artifact from `SignalApprovalGate` is the authorization for execution
- Blast radius is constrained at design time: `paper_trading_only`, `max_equity_price`, idempotency keys
- Forensic audit is post-chain, not in-chain — `ExecutionAuditWriter` runs regardless of pipeline outcome

---

## Full Chain

```
[Phase 2a — unchanged]
MessageNormalizer
TradeSignalExtractor
IdempotencyCheck
TickerResolver
ConvictionClassifier
ParsedSignalWriter
SignalDispositionResolver
SignalApprovalGate              ← emits immutable approval artifact
MarketHoursGuard                ← Phase 2a execution eligibility gate

[Phase 2b — new]
ExecutionEligibilityGuard       ← calendar / session / queue-or-reject
ChainLookup                     ← fetch + normalize option candidates from IB
InstrumentMarketabilityGuard    ← options window, spread sanity, fallback eligibility
ContractSelector                ← pure policy-driven contract selection
OrderSizer                      ← size from live buying power + conviction policy
OrderPricer                     ← build limit order per pricing_policy
OrderSubmitter                  ← place order with idempotency key
FillWaiter                      ← await fill with 30s timeout, persist durable state

[Post-chain hook — always runs]
ExecutionAuditWriter            ← immutable forensic snapshot to execution_audit_log

[Background — outside main chain]
ExecutionReconciler             ← reconcile timed-out/uncertain orders against broker
```

---

## Section 1: File Structure

### New files

| File | Responsibility |
|---|---|
| `infra/ib/__init__.py` | Package marker |
| `infra/ib/gateway.py` | `IBGateway` adapter, circuit breaker, broker object translation |
| `infra/ib/models.py` | `OptionCandidate`, `BrokerContractRef`, `AccountSummary`, `FillResult`, `FillStatus`, `ExecutionMode`, `PreparedOrder` |
| `infra/storage/execution_store.py` | Insert + update for `executions` and `execution_audit_log` tables |
| `skills/execution/__init__.py` | Package marker |
| `skills/execution/execution_eligibility_guard.py` | Session/calendar gate |
| `skills/execution/chain_lookup.py` | IB chain fetch, candidate persistence |
| `skills/execution/instrument_marketability_guard.py` | Instrument class eligibility |
| `skills/execution/contract_selector.py` | Policy-driven contract selection |
| `skills/execution/order_sizer.py` | Conviction-based sizing from live buying power |
| `skills/execution/order_pricer.py` | Limit price construction per pricing_policy |
| `skills/execution/order_submitter.py` | Order placement with idempotency |
| `skills/execution/fill_waiter.py` | Fill polling with durable state |
| `skills/execution/execution_audit_writer.py` | Post-chain forensic snapshot |
| `skills/execution/execution_reconciler.py` | Background reconciliation of uncertain orders |
| `tests/unit/test_execution_eligibility_guard.py` | Unit tests |
| `tests/unit/test_chain_lookup.py` | Unit tests (mock gateway) |
| `tests/unit/test_instrument_marketability_guard.py` | Unit tests |
| `tests/unit/test_contract_selector.py` | Unit tests |
| `tests/unit/test_order_sizer.py` | Unit tests |
| `tests/unit/test_order_pricer.py` | Unit tests |
| `tests/unit/test_order_submitter.py` | Unit tests (mock gateway) |
| `tests/unit/test_fill_waiter.py` | Unit tests (mock gateway) |
| `tests/integration/test_execution_store.py` | Integration tests against in-memory DB |
| `tests/e2e/test_phase2b_execution_pipeline.py` | End-to-end pipeline test (mock gateway) |

### Modified files

| File | Change |
|---|---|
| `infra/storage/db.py` | Add `option_candidates`, `approval_artifacts`, `executions`, `execution_audit_log` tables |
| `agent/policy.py` | Add `IBGatewayPolicy`, `ExecutionPolicy` models |
| `config/policy.yaml` | Add `ib_gateway`, `execution` sections |
| `agent/registry.py` | Add `build_phase2b_execution_chain` |
| `main.py` | Use `build_phase2b_execution_chain`, register `ExecutionAuditWriter` post-chain hook, start `ExecutionReconciler` on startup |

---

## Section 2: Data Models

### `FillStatus` enum

```python
class FillStatus(str, Enum):
    FILLED = "filled"
    PARTIAL_FILL = "partial_fill"
    SUBMITTED_UNFILLED = "submitted_unfilled"
    TIMED_OUT_PENDING = "timed_out_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
```

### `ExecutionMode` enum

```python
class ExecutionMode(str, Enum):
    EXECUTE_NOW = "execute_now"
    QUEUE_FOR_SESSION = "queue_for_session"
    REJECT = "reject"
```

### `BrokerContractRef` dataclass

```python
@dataclass
class BrokerContractRef:
    symbol: str
    sec_type: str               # 'OPT' | 'STK'
    exchange: str
    currency: str
    con_id: int | None = None
    expiry: str | None = None   # YYYYMMDD
    strike: float | None = None
    right: str | None = None    # 'C' | 'P'
    multiplier: str | None = None
    local_symbol: str | None = None
    trading_class: str | None = None
    qualified: bool = False     # True after IBGateway.qualify() or get_chain()
```

Qualification invariant:
- Options: arrive `qualified=True` from `get_chain()`
- Equity: `qualified=False` on creation; `OrderSubmitter` calls `gateway.qualify()` before `place_order()`
- `place_order()` asserts `contract_ref.qualified` — submission never proceeds on unqualified contracts

### `OptionCandidate` dataclass

```python
@dataclass
class OptionCandidate:
    symbol: str
    expiry: str             # YYYY-MM-DD
    strike: float
    right: str              # 'C' | 'P'
    bid: float
    ask: float
    mid: float
    spread_pct: float
    open_interest: int | None
    volume: int | None
    multiplier: int         # 100
    contract_ref: BrokerContractRef   # qualified=True
```

### `AccountSummary` dataclass

```python
@dataclass
class AccountSummary:
    buying_power: float
    net_liquidation: float
    currency: str
```

### `FillResult` dataclass

```python
@dataclass
class FillResult:
    status: FillStatus
    broker_order_id: str
    perm_id: int | None
    submitted_qty: int
    filled_qty: int
    remaining_qty: int
    avg_fill_price: float | None
    last_status: str
    status_timestamp: str
```

### `PreparedOrder` dataclass

```python
@dataclass
class PreparedOrder:
    action: str         # 'BUY'
    quantity: int
    order_type: str     # 'LMT'
    limit_price: float
    tif: str            # 'DAY'
```

---

## Section 3: DB Schema

Added to `infra/storage/db.py` `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS option_candidates (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,         -- attributable to one pipeline run
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL,
    bid REAL, ask REAL, mid REAL,
    spread_pct REAL,
    open_interest INTEGER,
    volume INTEGER,
    multiplier INTEGER DEFAULT 100,
    contract_ref_json TEXT NOT NULL,  -- serialized BrokerContractRef
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_artifacts (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    decision TEXT NOT NULL,           -- approved | rejected | timeout | approved_simulated
    approver TEXT,                    -- telegram user id or 'system'
    signal_hash TEXT NOT NULL,
    approved_execution_mode TEXT,     -- paper | live
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    instrument_type TEXT NOT NULL,    -- option | equity
    ticker TEXT NOT NULL,
    contract_ref_json TEXT,
    quantity INTEGER,
    notional_estimate REAL,
    limit_price REAL,
    sizing_reason TEXT,
    capped_by TEXT,
    broker_order_id TEXT,
    perm_id INTEGER,
    status TEXT NOT NULL,             -- submitted_unfilled | filled | partial_fill
                                      -- timed_out_pending | cancelled | rejected
    filled_qty INTEGER DEFAULT 0,
    avg_fill_price REAL,
    idempotency_key TEXT NOT NULL,
    submitted_at TEXT,
    filled_at TEXT,
    last_known_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_audit_log (
    id TEXT PRIMARY KEY,
    execution_id TEXT,
    signal_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    ctx_snapshot_json TEXT NOT NULL,  -- full ctx at post-chain time
    pipeline_outcome TEXT NOT NULL,   -- success | failed | skipped
    created_at TEXT NOT NULL
);
```

### Context keys added through execution chain

| Skill | Keys added to ctx |
|---|---|
| `ExecutionEligibilityGuard` | `execution_mode`, `execution_session` |
| `ChainLookup` | `option_candidates` (list of `OptionCandidate`) |
| `InstrumentMarketabilityGuard` | `instrument_type` (`option`\|`equity`), `fallback_reason` |
| `ContractSelector` | `selected_contract` (`BrokerContractRef`), `selected_expiry`, `selected_strike` |
| `OrderSizer` | `quantity`, `notional_estimate`, `sizing_reason`, `capped_by` |
| `OrderPricer` | `limit_price`, `order_type` |
| `OrderSubmitter` | `broker_order_id`, `idempotency_key`, `execution_id` |
| `FillWaiter` | `fill_status`, `filled_qty`, `avg_fill_price`, `perm_id` |

---

## Section 4: `IBGateway` Adapter

**File:** `infra/ib/gateway.py`

Single containment seam for all `ib_insync` usage. No other file in the project imports `ib_insync`.

### Interface

```python
class IBGateway:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def qualify(self, contract_ref: BrokerContractRef) -> BrokerContractRef: ...

    # Safe reads — retryable, read circuit breaker applies
    async def get_chain(self, ticker: str) -> list[OptionCandidate]: ...
    async def get_account_summary(self) -> AccountSummary: ...   # live every call, fail closed
    async def get_quote(self, ticker: str) -> float: ...         # equity ask price, live

    # Writes — no silent retry, caller-supplied idempotency key required
    async def place_order(
        self,
        contract_ref: BrokerContractRef,    # must be qualified=True
        order: PreparedOrder,
        client_order_id: str,               # trace_id:OrderSubmitter:signal_id
    ) -> IBTrade: ...

    async def wait_fill(self, trade: IBTrade, timeout: float) -> FillResult: ...
```

### Circuit Breaker — two independent counters

| Counter | Covers | Trips on | Does NOT trip on |
|---|---|---|---|
| Read breaker | `get_chain`, `get_account_summary`, `get_quote` | connection refused, socket timeout, API disconnect | pacing violations (backoff instead) |
| Write breaker | `place_order` | connection refused, socket timeout, API disconnect | order reject (business failure), parameter validation error |
| `wait_fill` timeouts | **neither breaker** | — fill timeout is execution outcome, not infra health |

Both breakers: 3 consecutive qualifying failures → Open; 1 probe every 30s in Half-open state; success → Closed.

### Retry policy

- Safe reads (`get_chain`, `get_account_summary`, `get_quote`): up to 3 retries with exponential backoff
- `place_order`: no silent retry; idempotency key is the caller's responsibility; gateway may retry only on connection-level failure with the same `client_order_id`
- `wait_fill`: no retry — timeout produces a durable `FillStatus.TIMED_OUT_PENDING` state

### Paper-trading blast radius guard

`place_order()` blocks and raises `LiveTradingBlocked` unless **all** of:
1. `policy.ib_gateway.mode == "paper"`
2. `policy.ib_gateway.port == 7497`
3. IB account ID prefix is in `policy.ib_gateway.paper_account_prefixes`

Port alone is insufficient — all three checks must pass.

---

## Section 5: Execution Skills

All skills in `skills/execution/`. No `ib_insync` imports in this package.

### `ExecutionEligibilityGuard`

- Reads: current ET time (injected `time_fn`), `policy.market_hours`
- Outputs: `execution_mode: ExecutionMode`, `execution_session: str`
- Logic:
  - Within RTH → `EXECUTE_NOW`, `execution_session=rth`
  - Equity premarket (time ≥ `stock_premarket_start`, `stock_premarket_allowed=true`) → `EXECUTE_NOW`, `execution_session=premarket`
  - Equity afterhours (`stock_afterhours_queue=true`) → `QUEUE_FOR_SESSION`, `execution_session=afterhours`
  - Outside all windows → `execution_mode=REJECT` → `fail`

### `ChainLookup`

- Reads: `ticker` from ctx
- Calls: `gateway.get_chain(ticker)`
- Outputs: `option_candidates` list persisted to `option_candidates` table with `trace_id`
- Empty chain → continues (absence of options is not a broker failure; `InstrumentMarketabilityGuard` handles fallback)
- Gateway circuit open → `fail` with `broker_unavailable`

### `InstrumentMarketabilityGuard`

- Reads: `option_candidates`, `execution_session`, policy
- Outputs: `instrument_type` (`option` | `equity`), `fallback_reason: str | None`
- Fallback to equity when:
  - Options outside RTH (session ≠ `rth`)
  - All candidates fail spread sanity (`spread_pct > max_spread_pct`)
  - Empty candidate list
- No candidates and no fallback → `fail`
- **Boundary:** strike preference and expiry ranking stay in `ContractSelector`. This skill answers "what instrument class is viable now?" only.

### `ContractSelector`

- Reads: `option_candidates`, `instrument_type`, policy
- Outputs: `selected_contract: BrokerContractRef`, `selected_expiry`, `selected_strike`
- Options path: apply `strike_policy` (closest ITM call), `min_expiry_days`, `min_bid`, `max_spread_pct` — pick best or `fail` with `no_eligible_contract`
- Equity fallback path: returns concrete equity `BrokerContractRef(symbol, sec_type="STK", exchange="SMART", currency="USD", qualified=False)`
- Every executable path ends with a concrete `BrokerContractRef` — no `None` downstream

### `OrderSizer`

Structured inputs (read from ctx + gateway):
- `account_summary` — fetched live via `gateway.get_account_summary()`; fails closed if unavailable
- `instrument_type` — `option` | `equity`
- `ask` price — from selected candidate (options) or `gateway.get_quote(ticker)` (equity)
- `multiplier` — from selected candidate (options) or 1 (equity)
- `conviction_bucket` — `low` | `high` from ctx
- `low_conviction_pct` / `high_conviction_pct` — from policy

Structured outputs:
```python
quantity: int
notional_estimate: float
sizing_reason: str          # e.g. "high_conviction 10% of $82,400 buying_power"
capped_by: str | None       # e.g. "max_single_order_notional" (future policy guard)
```

- Formula: `allocation = buying_power × conviction_pct`; `quantity = floor(allocation / (ask × multiplier))`
- `quantity < 1` → `fail` with `insufficient_buying_power`

### `OrderPricer`

- Reads: `instrument_type`, candidate `bid`/`ask`, policy `pricing_policy`
- Outputs: `limit_price: float`, `order_type: str` (`LMT`)
- Options: `mid + (ask - mid) × option_spread_fraction`, rounded to $0.01
- Equity: `ask × (1 + stock_buffer_pct)`
- Guards (options only): `min_bid`, `max_spread_pct` — `fail` if violated
- Guards (equity only): `ask > 0`, `ask < policy.execution.max_equity_price` (default $500) — `fail` if violated

### `OrderSubmitter`

- Reads: `selected_contract`, `quantity`, `limit_price`, `trace_id`, `signal_id`
- Builds: `client_order_id = f"{trace_id}:OrderSubmitter:{signal_id}"`
- Qualification: options assert `contract_ref.qualified=True`; equity calls `gateway.qualify(contract_ref)` before submit
- Calls: `gateway.place_order(contract_ref, prepared_order, client_order_id)`
- Writes: initial `executions` row with `status=submitted_unfilled`
- Gateway write breaker open → `fail` with `broker_unavailable`

### `FillWaiter`

- Reads: `broker_order_id`, `execution_id`
- Calls: `gateway.wait_fill(trade, timeout=30.0)`
- Persists `FillResult` to `executions` row regardless of outcome
- `TIMED_OUT_PENDING` → `success` with warning log — order exists at broker, `ExecutionReconciler` will resolve
- `REJECTED` → `fail` with broker rejection reason
- Fill timeouts do not trip circuit breaker

### `ExecutionAuditWriter` (post-chain hook)

- Runs after every pipeline completion, regardless of outcome (success, fail, skip)
- Writes immutable row to `execution_audit_log`: full ctx snapshot + pipeline outcome
- This is the forensic record. `executions` is the live lifecycle record.

### `ExecutionReconciler` (background, outside main chain)

- Starts on agent startup, runs periodically (interval: configurable, default 60s)
- Queries `executions` where `status IN ('submitted_unfilled', 'timed_out_pending')`
- Calls IB for current order status per `broker_order_id`
- Updates `executions` row with resolved `FillStatus`
- Prevents re-submission on restart via idempotency key check against existing rows

---

## Section 6: Policy Additions

```yaml
ib_gateway:
  host: "127.0.0.1"
  port: 7497                    # 7497 = paper, 7496 = live
  client_id: 1
  mode: paper                   # paper | live
  paper_account_prefixes:
    - "DU"                      # IB paper account prefix (configurable)

execution:
  fill_wait_timeout_seconds: 30
  max_equity_price: 500.0       # blast radius: reject equity orders above this ask price
  reconciler_interval_seconds: 60
```

Pydantic models added to `agent/policy.py`:

```python
class IBGatewayPolicy(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    mode: str = "paper"
    paper_account_prefixes: list[str] = ["DU"]

class ExecutionPolicy(BaseModel):
    fill_wait_timeout_seconds: float = 30.0
    max_equity_price: float = 500.0
    reconciler_interval_seconds: int = 60
```

---

## Section 7: Safety Invariants

These invariants are absolute — code must enforce them, this file names them.

| Invariant | Enforced by |
|---|---|
| Only `gateway.py` imports `ib_insync` | Import boundary (enforced by convention + future lint rule) |
| `place_order()` only accepts `qualified=True` contracts | `IBGateway.place_order()` assertion |
| No live orders when `mode=paper` | `IBGateway` blast radius guard (mode + port + account prefix) |
| No silent write retries | `IBGateway` retry policy (reads only) |
| Every order submission has an idempotency key | `OrderSubmitter` required `client_order_id` at gateway boundary |
| Fill timeouts do not trip circuit breaker | Explicit exclusion in breaker counters |
| `ExecutionAuditWriter` always runs | Post-chain hook, not a chain skill |
| `executions` row written before `place_order()` returns | `OrderSubmitter` write-before-submit ordering |

---

## Out of Scope (Phase 2c+)

- Live trading (`mode=live`)
- `RegimeCatalystUpgrader` (deferred — requires market data + EMA infrastructure)
- Position sizing from existing open positions (requires `PositionRegistry` as live view)
- Options spread strategies (single-leg only in this phase)
- Repricing / order amendment after submission
- Quote-refresh for options before pricing (staleness hardening — future)
- `qualified_at` timestamp on `BrokerContractRef` (session-validity hardening — future)
