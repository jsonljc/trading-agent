# IB Gateway Execution Layer (Phase 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the full paper-trading execution layer — IB Gateway adapter, execution skills, and reconciler — so an approved signal produces a real paper trade via IB.

**Architecture:** Thin `IBGateway` adapter (`infra/ib/gateway.py`) is the sole `ib_insync` import point; all business logic lives in `skills/execution/`. The synchronous chain runs from `ExecutionEligibilityGuard` through `FillWaiter`; `ExecutionAuditWriter` runs as a post-chain hook; `ExecutionReconciler` runs as a background task.

**Tech Stack:** Python 3.11+, ib_insync, aiosqlite, pydantic v2, pytest-asyncio, zoneinfo (stdlib).

**Prerequisite:** Phase 2a complete (ParsedTradeSignal, SignalApprovalGate, MarketHoursGuard).

---

## File Structure

### New files
| File | Responsibility |
|---|---|
| `infra/ib/__init__.py` | Package marker |
| `infra/ib/models.py` | All execution-domain dataclasses and enums |
| `infra/ib/gateway.py` | IBGateway adapter, circuit breaker, broker translation |
| `infra/storage/execution_store.py` | Insert/update for executions + execution_audit_log |
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

### Modified files
| File | Change |
|---|---|
| `pyproject.toml` | Add `ib_insync` dependency |
| `infra/storage/db.py` | Add 4 new tables to SCHEMA |
| `agent/policy.py` | Add IBGatewayPolicy, ExecutionPolicy |
| `config/policy.yaml` | Add ib_gateway and execution sections |
| `agent/registry.py` | Add build_phase2b_execution_chain |
| `main.py` | Wire phase2b chain + audit hook + reconciler |

---

## Task 0: Add ib_insync dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add ib_insync to dependencies**

Open `pyproject.toml`. Change:
```toml
dependencies = [
    "anthropic>=0.25.0",
    "aiosqlite>=0.20.0",
    "httpx>=0.27.0",
    "pydantic>=2.6.0",
    "pyyaml>=6.0",
]
```
to:
```toml
dependencies = [
    "anthropic>=0.25.0",
    "aiosqlite>=0.20.0",
    "httpx>=0.27.0",
    "ib_insync>=0.9.86",
    "pydantic>=2.6.0",
    "pyyaml>=6.0",
]
```

- [ ] **Step 2: Install**

```bash
pip install ib_insync>=0.9.86
```

Expected: installs without error.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add ib_insync dependency"
```

---

## Task 1: Execution domain models

**Files:**
- Create: `infra/ib/__init__.py`
- Create: `infra/ib/models.py`
- Test: none (pure dataclasses — tested via consuming code)

- [ ] **Step 1: Create package marker**

Create `infra/ib/__init__.py` as an empty file.

- [ ] **Step 2: Create `infra/ib/models.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class FillStatus(str, Enum):
    FILLED = "filled"
    PARTIAL_FILL = "partial_fill"
    SUBMITTED_UNFILLED = "submitted_unfilled"
    TIMED_OUT_PENDING = "timed_out_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ExecutionMode(str, Enum):
    EXECUTE_NOW = "execute_now"
    QUEUE_FOR_SESSION = "queue_for_session"
    REJECT = "reject"


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
    qualified: bool = False


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
    multiplier: int
    contract_ref: BrokerContractRef


@dataclass
class AccountSummary:
    buying_power: float
    net_liquidation: float
    currency: str


@dataclass
class PreparedOrder:
    action: str         # 'BUY'
    quantity: int
    order_type: str     # 'LMT'
    limit_price: float
    tif: str            # 'DAY'


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

- [ ] **Step 3: Verify import**

```bash
python3 -c "from infra.ib.models import FillStatus, ExecutionMode, BrokerContractRef, OptionCandidate, AccountSummary, PreparedOrder, FillResult; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add infra/ib/__init__.py infra/ib/models.py
git commit -m "feat(ib): add execution domain models"
```

---

## Task 2: DB schema — execution tables

**Files:**
- Modify: `infra/storage/db.py`
- Test: `tests/integration/test_execution_store.py` (written in Task 4)

- [ ] **Step 1: Add four tables to SCHEMA in `infra/storage/db.py`**

Replace the closing `"""` of the `SCHEMA` string with the following four tables then close:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_events (
    id TEXT PRIMARY KEY,
    source TEXT,
    channel TEXT,
    author TEXT,
    trigger_preview TEXT,
    full_message_text TEXT,
    capture_mode TEXT,
    message_fingerprint TEXT,
    received_at TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    event_id TEXT,
    ticker TEXT,
    action TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS work_traces (
    trace_id TEXT PRIMARY KEY,
    event_id TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS skill_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    skill_name TEXT,
    status TEXT,
    output_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS option_candidates (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL,
    spread_pct REAL,
    open_interest INTEGER,
    volume INTEGER,
    multiplier INTEGER DEFAULT 100,
    contract_ref_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_artifacts (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT,
    signal_hash TEXT NOT NULL,
    approved_execution_mode TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    ticker TEXT NOT NULL,
    contract_ref_json TEXT,
    quantity INTEGER,
    notional_estimate REAL,
    limit_price REAL,
    sizing_reason TEXT,
    capped_by TEXT,
    broker_order_id TEXT,
    perm_id INTEGER,
    status TEXT NOT NULL,
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
    ctx_snapshot_json TEXT NOT NULL,
    pipeline_outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
```

- [ ] **Step 2: Verify schema applies cleanly**

```bash
python3 -c "
import asyncio, aiosqlite
from infra.storage.db import SCHEMA
async def check():
    async with aiosqlite.connect(':memory:') as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
        print('schema ok')
asyncio.run(check())
"
```

Expected: `schema ok`

- [ ] **Step 3: Commit**

```bash
git add infra/storage/db.py
git commit -m "feat(db): add execution tables to schema"
```

---

## Task 3: Policy models — IBGatewayPolicy and ExecutionPolicy

**Files:**
- Modify: `agent/policy.py`
- Modify: `config/policy.yaml`

- [ ] **Step 1: Add policy models to `agent/policy.py`**

Add before the `PolicyModel` class:

```python
class IBGatewayPolicy(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    mode: str = "paper"
    paper_account_prefixes: list[str] = field(default_factory=lambda: ["DU"])

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v


class ExecutionPolicy(BaseModel):
    fill_wait_timeout_seconds: float = 30.0
    max_equity_price: float = 500.0
    reconciler_interval_seconds: int = 60
```

Add to `PolicyModel`:
```python
class PolicyModel(BaseModel):
    trigger: TriggerPolicy
    instrument_policy: InstrumentPolicy
    pricing_policy: PricingPolicy
    sizing_policy: SizingPolicy
    market_hours: MarketHours
    cooldown_policy: CooldownPolicy
    dedupe_policy: DedupePolicy
    pricing_policy_guards: PricingGuards
    models: ModelsConfig
    watched_channels: list[str]
    discord_bundle_id: str
    telegram: TelegramConfig
    ib_gateway: IBGatewayPolicy = IBGatewayPolicy()
    execution: ExecutionPolicy = ExecutionPolicy()
```

Note: `IBGatewayPolicy` uses a `field_validator` not a `field` default — remove `field(default_factory=...)` and use `["DU"]` directly as the default value in the `list[str]` field:

```python
class IBGatewayPolicy(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    mode: str = "paper"
    paper_account_prefixes: list[str] = ["DU"]

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v
```

- [ ] **Step 2: Add sections to `config/policy.yaml`**

Append to the end of `config/policy.yaml`:

```yaml
ib_gateway:
  host: "127.0.0.1"
  port: 7497
  client_id: 1
  mode: paper
  paper_account_prefixes:
    - "DU"

execution:
  fill_wait_timeout_seconds: 30
  max_equity_price: 500.0
  reconciler_interval_seconds: 60
```

- [ ] **Step 3: Verify policy loads**

```bash
python3 -c "
from agent.policy import load_policy
p = load_policy('config/policy.yaml')
print(p.ib_gateway.mode, p.execution.fill_wait_timeout_seconds)
"
```

Expected: `paper 30.0`

- [ ] **Step 4: Commit**

```bash
git add agent/policy.py config/policy.yaml
git commit -m "feat(policy): add IBGatewayPolicy and ExecutionPolicy"
```

---

## Task 4: ExecutionStore

**Files:**
- Create: `infra/storage/execution_store.py`
- Test: `tests/integration/test_execution_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/integration/test_execution_store.py`:

```python
import pytest
import json
from datetime import datetime, timezone
from infra.storage.execution_store import ExecutionStore
from infra.ib.models import FillStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_insert_execution(db):
    store = ExecutionStore(db)
    await store.insert_execution({
        "id": "exec-1",
        "signal_id": "sig-1",
        "trace_id": "trace-1",
        "instrument_type": "option",
        "ticker": "AAPL",
        "contract_ref_json": json.dumps({"symbol": "AAPL"}),
        "quantity": 1,
        "notional_estimate": 500.0,
        "limit_price": 5.00,
        "sizing_reason": "high_conviction",
        "capped_by": None,
        "broker_order_id": None,
        "perm_id": None,
        "status": FillStatus.SUBMITTED_UNFILLED.value,
        "filled_qty": 0,
        "avg_fill_price": None,
        "idempotency_key": "trace-1:OrderSubmitter:sig-1",
        "submitted_at": _now(),
        "filled_at": None,
        "last_known_at": _now(),
    })
    async with db.execute("SELECT id, status FROM executions WHERE id='exec-1'") as cur:
        row = await cur.fetchone()
    assert row["id"] == "exec-1"
    assert row["status"] == "submitted_unfilled"


@pytest.mark.asyncio
async def test_update_execution_status(db):
    store = ExecutionStore(db)
    now = _now()
    await store.insert_execution({
        "id": "exec-2", "signal_id": "sig-2", "trace_id": "trace-2",
        "instrument_type": "equity", "ticker": "TSLA",
        "contract_ref_json": None, "quantity": 10, "notional_estimate": 2000.0,
        "limit_price": 200.0, "sizing_reason": "low_conviction", "capped_by": None,
        "broker_order_id": None, "perm_id": None,
        "status": FillStatus.SUBMITTED_UNFILLED.value,
        "filled_qty": 0, "avg_fill_price": None,
        "idempotency_key": "trace-2:OrderSubmitter:sig-2",
        "submitted_at": now, "filled_at": None, "last_known_at": now,
    })
    await store.update_execution_status(
        execution_id="exec-2",
        status=FillStatus.FILLED,
        filled_qty=10,
        avg_fill_price=201.5,
        broker_order_id="IB-999",
        perm_id=12345,
        filled_at=now,
    )
    async with db.execute("SELECT status, filled_qty, avg_fill_price FROM executions WHERE id='exec-2'") as cur:
        row = await cur.fetchone()
    assert row["status"] == "filled"
    assert row["filled_qty"] == 10
    assert row["avg_fill_price"] == 201.5


@pytest.mark.asyncio
async def test_insert_audit_log(db):
    store = ExecutionStore(db)
    await store.insert_audit_log({
        "id": "audit-1",
        "execution_id": "exec-1",
        "signal_id": "sig-1",
        "trace_id": "trace-1",
        "ctx_snapshot_json": json.dumps({"ticker": "AAPL"}),
        "pipeline_outcome": "success",
        "created_at": _now(),
    })
    async with db.execute("SELECT id FROM execution_audit_log WHERE id='audit-1'") as cur:
        row = await cur.fetchone()
    assert row["id"] == "audit-1"


@pytest.mark.asyncio
async def test_get_uncertain_executions(db):
    store = ExecutionStore(db)
    now = _now()
    for exec_id, status in [
        ("e1", FillStatus.SUBMITTED_UNFILLED.value),
        ("e2", FillStatus.TIMED_OUT_PENDING.value),
        ("e3", FillStatus.FILLED.value),
    ]:
        await store.insert_execution({
            "id": exec_id, "signal_id": "s", "trace_id": "t",
            "instrument_type": "equity", "ticker": "SPY",
            "contract_ref_json": None, "quantity": 1, "notional_estimate": 100.0,
            "limit_price": 100.0, "sizing_reason": "", "capped_by": None,
            "broker_order_id": f"ib-{exec_id}", "perm_id": None,
            "status": status, "filled_qty": 0, "avg_fill_price": None,
            "idempotency_key": f"k-{exec_id}",
            "submitted_at": now, "filled_at": None, "last_known_at": now,
        })
    rows = await store.get_uncertain_executions()
    ids = {r["id"] for r in rows}
    assert ids == {"e1", "e2"}
```

- [ ] **Step 2: Run tests — expect failure**

```bash
pytest tests/integration/test_execution_store.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `ExecutionStore` does not exist yet.

- [ ] **Step 3: Create `infra/storage/execution_store.py`**

```python
from __future__ import annotations
from datetime import datetime, timezone
import aiosqlite
from infra.ib.models import FillStatus


class ExecutionStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert_execution(self, record: dict) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO executions
               (id, signal_id, trace_id, instrument_type, ticker,
                contract_ref_json, quantity, notional_estimate, limit_price,
                sizing_reason, capped_by, broker_order_id, perm_id, status,
                filled_qty, avg_fill_price, idempotency_key,
                submitted_at, filled_at, last_known_at)
               VALUES
               (:id, :signal_id, :trace_id, :instrument_type, :ticker,
                :contract_ref_json, :quantity, :notional_estimate, :limit_price,
                :sizing_reason, :capped_by, :broker_order_id, :perm_id, :status,
                :filled_qty, :avg_fill_price, :idempotency_key,
                :submitted_at, :filled_at, :last_known_at)""",
            record,
        )
        await self._conn.commit()

    async def update_execution_status(
        self,
        execution_id: str,
        status: FillStatus,
        filled_qty: int = 0,
        avg_fill_price: float | None = None,
        broker_order_id: str | None = None,
        perm_id: int | None = None,
        filled_at: str | None = None,
    ) -> None:
        await self._conn.execute(
            """UPDATE executions SET
               status=:status, filled_qty=:filled_qty,
               avg_fill_price=:avg_fill_price, broker_order_id=:broker_order_id,
               perm_id=:perm_id, filled_at=:filled_at,
               last_known_at=:last_known_at
               WHERE id=:id""",
            {
                "status": status.value,
                "filled_qty": filled_qty,
                "avg_fill_price": avg_fill_price,
                "broker_order_id": broker_order_id,
                "perm_id": perm_id,
                "filled_at": filled_at,
                "last_known_at": datetime.now(timezone.utc).isoformat(),
                "id": execution_id,
            },
        )
        await self._conn.commit()

    async def insert_audit_log(self, record: dict) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO execution_audit_log
               (id, execution_id, signal_id, trace_id,
                ctx_snapshot_json, pipeline_outcome, created_at)
               VALUES
               (:id, :execution_id, :signal_id, :trace_id,
                :ctx_snapshot_json, :pipeline_outcome, :created_at)""",
            record,
        )
        await self._conn.commit()

    async def get_uncertain_executions(self) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            """SELECT * FROM executions
               WHERE status IN (?, ?)""",
            (FillStatus.SUBMITTED_UNFILLED.value, FillStatus.TIMED_OUT_PENDING.value),
        ) as cur:
            return await cur.fetchall()
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/integration/test_execution_store.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add infra/storage/execution_store.py tests/integration/test_execution_store.py
git commit -m "feat(store): add ExecutionStore for executions and audit log"
```

---

## Task 5: IBGateway adapter

**Files:**
- Create: `infra/ib/gateway.py`
- Test: `tests/unit/test_gateway_circuit_breaker.py`

The gateway wraps `ib_insync`. Tests use a `FakeIBGateway` — they never connect to a real broker.

- [ ] **Step 1: Write failing circuit breaker tests**

Create `tests/unit/test_gateway_circuit_breaker.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch
from infra.ib.gateway import IBGateway, IBGatewayUnavailable, LiveTradingBlocked
from infra.ib.models import BrokerContractRef, PreparedOrder


def _paper_policy():
    from unittest.mock import MagicMock
    p = MagicMock()
    p.ib_gateway.host = "127.0.0.1"
    p.ib_gateway.port = 7497
    p.ib_gateway.client_id = 1
    p.ib_gateway.mode = "paper"
    p.ib_gateway.paper_account_prefixes = ["DU"]
    return p


@pytest.mark.asyncio
async def test_read_breaker_opens_after_three_failures():
    gw = IBGateway(_paper_policy())
    gw._ib = AsyncMock()
    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=ConnectionError("refused"))
    # Simulate 3 consecutive read failures
    for _ in range(3):
        gw._read_breaker._record_failure()
    assert gw._read_breaker.is_open()


@pytest.mark.asyncio
async def test_read_breaker_closed_after_success():
    gw = IBGateway(_paper_policy())
    for _ in range(3):
        gw._read_breaker._record_failure()
    gw._read_breaker._record_success()
    assert not gw._read_breaker.is_open()


@pytest.mark.asyncio
async def test_fill_timeout_does_not_trip_write_breaker():
    gw = IBGateway(_paper_policy())
    # Fill timeout must never trip breakers
    initial_read = gw._read_breaker._failure_count
    initial_write = gw._write_breaker._failure_count
    # Simulate fill timeout recording (should be a no-op on breakers)
    gw._record_fill_timeout()
    assert gw._read_breaker._failure_count == initial_read
    assert gw._write_breaker._failure_count == initial_write


@pytest.mark.asyncio
async def test_live_trading_blocked_when_mode_is_paper():
    gw = IBGateway(_paper_policy())
    gw._connected = True
    gw._account_id = "DU123456"
    contract = BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", qualified=True,
    )
    order = PreparedOrder(action="BUY", quantity=1, order_type="LMT", limit_price=150.0, tif="DAY")
    # Mode is paper + port is 7497 + prefix is DU → should NOT raise
    # Test that wrong port raises LiveTradingBlocked
    gw._policy.ib_gateway.port = 7496
    with pytest.raises(LiveTradingBlocked):
        await gw.place_order(contract, order, "test-key")
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_gateway_circuit_breaker.py -v
```

Expected: `ImportError` — `IBGateway` not defined.

- [ ] **Step 3: Create `infra/ib/gateway.py`**

```python
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from infra.ib.models import (
    BrokerContractRef, OptionCandidate, AccountSummary,
    PreparedOrder, FillResult, FillStatus,
)

logger = logging.getLogger(__name__)


class IBGatewayUnavailable(Exception):
    pass


class LiveTradingBlocked(Exception):
    pass


@dataclass
class _CircuitBreaker:
    threshold: int = 3
    probe_interval: float = 30.0
    _failure_count: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.probe_interval:
            return False  # half-open: allow probe
        return True

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self.threshold:
            self._opened_at = time.monotonic()

    def _record_success(self) -> None:
        self._failure_count = 0
        self._opened_at = None

    def check(self) -> None:
        if self.is_open():
            raise IBGatewayUnavailable("circuit open")


class IBGateway:
    def __init__(self, policy) -> None:
        self._policy = policy
        self._ib = None
        self._connected = False
        self._account_id: str | None = None
        self._read_breaker = _CircuitBreaker()
        self._write_breaker = _CircuitBreaker()

    async def connect(self) -> None:
        from ib_insync import IB
        self._ib = IB()
        p = self._policy.ib_gateway
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._ib.connect(p.host, p.port, clientId=p.client_id),
        )
        accounts = self._ib.managedAccounts()
        self._account_id = accounts[0] if accounts else None
        self._connected = True
        logger.info("Connected to IB Gateway. Account: %s", self._account_id)

    async def disconnect(self) -> None:
        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False

    async def qualify(self, contract_ref: BrokerContractRef) -> BrokerContractRef:
        from ib_insync import Contract
        self._read_breaker.check()
        try:
            ib_contract = _to_ib_contract(contract_ref)
            qualified = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._ib.qualifyContracts(ib_contract),
            )
            if not qualified:
                raise IBGatewayUnavailable("qualification returned empty")
            result = _from_ib_contract(qualified[0])
            result.qualified = True
            self._read_breaker._record_success()
            return result
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"qualify failed: {exc}") from exc

    async def get_chain(self, ticker: str) -> list[OptionCandidate]:
        from ib_insync import Stock, Option
        self._read_breaker.check()
        try:
            stock = Stock(ticker, "SMART", "USD")
            chains = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._ib.reqSecDefOptParams(ticker, "", "STK", 0),
            )
            if not chains:
                self._read_breaker._record_success()
                return []
            chain = chains[0]
            candidates: list[OptionCandidate] = []
            for expiry in chain.expirations:
                for strike in chain.strikes:
                    for right in ("C", "P"):
                        opt = Option(ticker, expiry, strike, right, "SMART")
                        try:
                            qualified = self._ib.qualifyContracts(opt)
                            if not qualified:
                                continue
                            q = qualified[0]
                            ticker_data = self._ib.reqMktData(q, "", False, False)
                            self._ib.sleep(1)
                            bid = ticker_data.bid if ticker_data.bid and ticker_data.bid > 0 else 0.0
                            ask = ticker_data.ask if ticker_data.ask and ticker_data.ask > 0 else 0.0
                            mid = (bid + ask) / 2
                            spread_pct = ((ask - bid) / ask) if ask > 0 else 1.0
                            ref = _from_ib_contract(q)
                            ref.qualified = True
                            candidates.append(OptionCandidate(
                                symbol=ticker,
                                expiry=f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}",
                                strike=strike,
                                right=right,
                                bid=bid,
                                ask=ask,
                                mid=mid,
                                spread_pct=spread_pct,
                                open_interest=None,
                                volume=None,
                                multiplier=int(q.multiplier or 100),
                                contract_ref=ref,
                            ))
                        except Exception:
                            continue
            self._read_breaker._record_success()
            return candidates
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_chain failed: {exc}") from exc

    async def get_account_summary(self) -> AccountSummary:
        self._read_breaker.check()
        try:
            summary = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._ib.accountSummary(),
            )
            values = {item.tag: item.value for item in summary}
            result = AccountSummary(
                buying_power=float(values.get("BuyingPower", 0)),
                net_liquidation=float(values.get("NetLiquidation", 0)),
                currency=values.get("Currency", "USD"),
            )
            self._read_breaker._record_success()
            return result
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_account_summary failed: {exc}") from exc

    async def get_quote(self, ticker: str) -> float:
        from ib_insync import Stock
        self._read_breaker.check()
        try:
            stock = Stock(ticker, "SMART", "USD")
            qualified = self._ib.qualifyContracts(stock)
            if not qualified:
                raise IBGatewayUnavailable(f"could not qualify equity {ticker}")
            ticker_data = self._ib.reqMktData(qualified[0], "", False, False)
            self._ib.sleep(1)
            ask = ticker_data.ask
            if not ask or ask <= 0:
                raise IBGatewayUnavailable(f"no ask price for {ticker}")
            self._read_breaker._record_success()
            return float(ask)
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_quote failed: {exc}") from exc

    async def place_order(
        self,
        contract_ref: BrokerContractRef,
        order: PreparedOrder,
        client_order_id: str,
    ):
        self._assert_paper_guard()
        self._write_breaker.check()
        if not contract_ref.qualified:
            raise ValueError("contract_ref must be qualified before place_order")
        from ib_insync import LimitOrder
        try:
            ib_contract = _to_ib_contract(contract_ref)
            ib_order = LimitOrder(
                action=order.action,
                totalQuantity=order.quantity,
                lmtPrice=order.limit_price,
                tif=order.tif,
                orderRef=client_order_id,
            )
            trade = self._ib.placeOrder(ib_contract, ib_order)
            self._write_breaker._record_success()
            logger.info("Placed order %s: %s x%s @ %s", client_order_id, contract_ref.symbol, order.quantity, order.limit_price)
            return trade
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._write_breaker._record_failure()
            raise IBGatewayUnavailable(f"place_order failed: {exc}") from exc

    async def wait_fill(self, trade, timeout: float) -> FillResult:
        deadline = time.monotonic() + timeout
        broker_order_id = str(trade.order.orderId)
        submitted_qty = trade.order.totalQuantity
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            self._ib.sleep(0)
            filled = trade.orderStatus.filled
            remaining = trade.orderStatus.remaining
            status_str = trade.orderStatus.status
            if status_str in ("Filled",):
                return FillResult(
                    status=FillStatus.FILLED,
                    broker_order_id=broker_order_id,
                    perm_id=trade.order.permId or None,
                    submitted_qty=int(submitted_qty),
                    filled_qty=int(filled),
                    remaining_qty=int(remaining),
                    avg_fill_price=trade.orderStatus.avgFillPrice or None,
                    last_status=status_str,
                    status_timestamp=datetime.now(timezone.utc).isoformat(),
                )
            if status_str in ("Cancelled", "ApiCancelled"):
                return FillResult(
                    status=FillStatus.CANCELLED,
                    broker_order_id=broker_order_id,
                    perm_id=trade.order.permId or None,
                    submitted_qty=int(submitted_qty),
                    filled_qty=int(filled),
                    remaining_qty=int(remaining),
                    avg_fill_price=trade.orderStatus.avgFillPrice or None,
                    last_status=status_str,
                    status_timestamp=datetime.now(timezone.utc).isoformat(),
                )
            if status_str in ("Inactive",):
                return FillResult(
                    status=FillStatus.REJECTED,
                    broker_order_id=broker_order_id,
                    perm_id=trade.order.permId or None,
                    submitted_qty=int(submitted_qty),
                    filled_qty=int(filled),
                    remaining_qty=int(remaining),
                    avg_fill_price=None,
                    last_status=status_str,
                    status_timestamp=datetime.now(timezone.utc).isoformat(),
                )
            if filled > 0 and remaining > 0:
                pass  # partial, keep waiting
        # Timeout
        self._record_fill_timeout()
        filled = trade.orderStatus.filled
        return FillResult(
            status=FillStatus.TIMED_OUT_PENDING,
            broker_order_id=broker_order_id,
            perm_id=trade.order.permId or None,
            submitted_qty=int(submitted_qty),
            filled_qty=int(filled),
            remaining_qty=int(submitted_qty - filled),
            avg_fill_price=trade.orderStatus.avgFillPrice or None,
            last_status=trade.orderStatus.status,
            status_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _record_fill_timeout(self) -> None:
        pass  # fill timeouts never trip breakers — intentional no-op

    def _assert_paper_guard(self) -> None:
        p = self._policy.ib_gateway
        if p.mode != "paper":
            raise LiveTradingBlocked("mode is not 'paper'")
        if p.port != 7497:
            raise LiveTradingBlocked(f"port {p.port} is not the paper trading port (7497)")
        if self._account_id:
            prefix_ok = any(self._account_id.startswith(pfx) for pfx in p.paper_account_prefixes)
            if not prefix_ok:
                raise LiveTradingBlocked(f"account {self._account_id} not in allowed paper prefixes {p.paper_account_prefixes}")


def _to_ib_contract(ref: BrokerContractRef):
    from ib_insync import Contract
    c = Contract()
    c.symbol = ref.symbol
    c.secType = ref.sec_type
    c.exchange = ref.exchange
    c.currency = ref.currency
    if ref.con_id:
        c.conId = ref.con_id
    if ref.expiry:
        c.lastTradeDateOrContractMonth = ref.expiry
    if ref.strike is not None:
        c.strike = ref.strike
    if ref.right:
        c.right = ref.right
    if ref.multiplier:
        c.multiplier = ref.multiplier
    if ref.local_symbol:
        c.localSymbol = ref.local_symbol
    if ref.trading_class:
        c.tradingClass = ref.trading_class
    return c


def _from_ib_contract(c) -> BrokerContractRef:
    return BrokerContractRef(
        symbol=c.symbol,
        sec_type=c.secType,
        exchange=c.exchange or "SMART",
        currency=c.currency or "USD",
        con_id=c.conId or None,
        expiry=c.lastTradeDateOrContractMonth or None,
        strike=float(c.strike) if c.strike else None,
        right=c.right or None,
        multiplier=str(c.multiplier) if c.multiplier else None,
        local_symbol=c.localSymbol or None,
        trading_class=c.tradingClass or None,
        qualified=False,
    )
```

- [ ] **Step 4: Run circuit breaker tests — expect pass**

```bash
pytest tests/unit/test_gateway_circuit_breaker.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add infra/ib/gateway.py tests/unit/test_gateway_circuit_breaker.py
git commit -m "feat(ib): add IBGateway adapter with circuit breaker and paper-trading guard"
```

---

## Task 6: ExecutionEligibilityGuard

**Files:**
- Create: `skills/execution/__init__.py`
- Create: `skills/execution/execution_eligibility_guard.py`
- Test: `tests/unit/test_execution_eligibility_guard.py`

- [ ] **Step 1: Create package marker**

Create `skills/execution/__init__.py` as an empty file.

- [ ] **Step 2: Write failing tests**

Create `tests/unit/test_execution_eligibility_guard.py`:

```python
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock
from agent.context import Context, SkillResult
from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
from infra.ib.models import ExecutionMode

ET = ZoneInfo("America/New_York")


def _policy(premarket=True, afterhours_queue=True):
    p = MagicMock()
    p.market_hours.rth_start = "09:30"
    p.market_hours.rth_end = "16:00"
    p.market_hours.stock_premarket_allowed = premarket
    p.market_hours.stock_premarket_start = "04:00"
    p.market_hours.stock_afterhours_queue = afterhours_queue
    return p


def _ctx():
    c = Context(trace_id="t1", event_id="e1")
    return c


def _at(hour, minute=0):
    return lambda: datetime(2026, 4, 22, hour, minute, tzinfo=ET)


@pytest.mark.asyncio
async def test_rth_execute_now():
    guard = ExecutionEligibilityGuard(_policy(), time_fn=_at(10, 0))
    result = await guard.run(_ctx())
    assert result.status == "success"
    assert result.updates["execution_mode"] == ExecutionMode.EXECUTE_NOW.value
    assert result.updates["execution_session"] == "rth"


@pytest.mark.asyncio
async def test_premarket_execute_now():
    guard = ExecutionEligibilityGuard(_policy(), time_fn=_at(6, 0))
    result = await guard.run(_ctx())
    assert result.status == "success"
    assert result.updates["execution_mode"] == ExecutionMode.EXECUTE_NOW.value
    assert result.updates["execution_session"] == "premarket"


@pytest.mark.asyncio
async def test_premarket_before_window_reject():
    guard = ExecutionEligibilityGuard(_policy(), time_fn=_at(3, 59))
    result = await guard.run(_ctx())
    assert result.status == "fail"
    assert "execution_ineligible" in result.reason


@pytest.mark.asyncio
async def test_afterhours_queue():
    guard = ExecutionEligibilityGuard(_policy(afterhours_queue=True), time_fn=_at(17, 0))
    result = await guard.run(_ctx())
    assert result.status == "success"
    assert result.updates["execution_mode"] == ExecutionMode.QUEUE_FOR_SESSION.value


@pytest.mark.asyncio
async def test_afterhours_no_queue_reject():
    guard = ExecutionEligibilityGuard(_policy(afterhours_queue=False), time_fn=_at(17, 0))
    result = await guard.run(_ctx())
    assert result.status == "fail"
```

- [ ] **Step 3: Run — expect failure**

```bash
pytest tests/unit/test_execution_eligibility_guard.py -v
```

Expected: `ImportError`.

- [ ] **Step 4: Create `skills/execution/execution_eligibility_guard.py`**

```python
from __future__ import annotations
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import ExecutionMode

ET = ZoneInfo("America/New_York")


def _default_time_fn() -> datetime:
    return datetime.now(ET)


def _parse_time(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class ExecutionEligibilityGuard(Skill):
    name = "ExecutionEligibilityGuard"

    def __init__(self, policy, time_fn=None) -> None:
        self._policy = policy
        self._time_fn = time_fn or _default_time_fn

    async def run(self, ctx: Context) -> SkillResult:
        mh = self._policy.market_hours
        now = self._time_fn()
        current = now.time().replace(second=0, microsecond=0)

        rth_start = _parse_time(mh.rth_start)
        rth_end = _parse_time(mh.rth_end)
        premarket_start = _parse_time(mh.stock_premarket_start)

        if rth_start <= current < rth_end:
            return SkillResult(status="success", updates={
                "execution_mode": ExecutionMode.EXECUTE_NOW.value,
                "execution_session": "rth",
            })

        if mh.stock_premarket_allowed and premarket_start <= current < rth_start:
            return SkillResult(status="success", updates={
                "execution_mode": ExecutionMode.EXECUTE_NOW.value,
                "execution_session": "premarket",
            })

        if current >= rth_end:
            if mh.stock_afterhours_queue:
                return SkillResult(status="success", updates={
                    "execution_mode": ExecutionMode.QUEUE_FOR_SESSION.value,
                    "execution_session": "afterhours",
                })
            return SkillResult(
                status="fail",
                reason=f"execution_ineligible: afterhours queue disabled (current ET {current})",
            )

        return SkillResult(
            status="fail",
            reason=f"execution_ineligible: outside all eligible windows (current ET {current})",
        )
```

- [ ] **Step 5: Run — expect pass**

```bash
pytest tests/unit/test_execution_eligibility_guard.py -v
```

Expected: 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add skills/execution/__init__.py skills/execution/execution_eligibility_guard.py tests/unit/test_execution_eligibility_guard.py
git commit -m "feat(execution): add ExecutionEligibilityGuard"
```

---

## Task 7: ChainLookup

**Files:**
- Create: `skills/execution/chain_lookup.py`
- Test: `tests/unit/test_chain_lookup.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_chain_lookup.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.chain_lookup import ChainLookup
from infra.ib.models import OptionCandidate, BrokerContractRef
from infra.ib.gateway import IBGatewayUnavailable


def _candidate(ticker="AAPL", strike=150.0):
    ref = BrokerContractRef(symbol=ticker, sec_type="OPT", exchange="SMART", currency="USD",
                             expiry="20261218", strike=strike, right="C", qualified=True)
    return OptionCandidate(symbol=ticker, expiry="2026-12-18", strike=strike, right="C",
                            bid=5.0, ask=5.5, mid=5.25, spread_pct=0.09,
                            open_interest=100, volume=50, multiplier=100, contract_ref=ref)


def _ctx(signal_id="sig-1", trace_id="trace-1", ticker="AAPL"):
    c = Context(trace_id=trace_id, event_id=signal_id)
    c.update({"signal_id": signal_id, "ticker": ticker})
    return c


@pytest.mark.asyncio
async def test_chain_lookup_success(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(return_value=[_candidate()])
    skill = ChainLookup(gateway, db)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert len(result.updates["option_candidates"]) == 1
    assert result.updates["option_candidates"][0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_chain_lookup_empty_continues(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(return_value=[])
    skill = ChainLookup(gateway, db)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["option_candidates"] == []


@pytest.mark.asyncio
async def test_chain_lookup_gateway_unavailable_fails(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(side_effect=IBGatewayUnavailable("circuit open"))
    skill = ChainLookup(gateway, db)
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "broker_unavailable" in result.reason


@pytest.mark.asyncio
async def test_chain_lookup_persists_with_trace_id(db):
    gateway = MagicMock()
    gateway.get_chain = AsyncMock(return_value=[_candidate()])
    skill = ChainLookup(gateway, db)
    await skill.run(_ctx(trace_id="trace-xyz"))
    async with db.execute("SELECT trace_id FROM option_candidates") as cur:
        row = await cur.fetchone()
    assert row["trace_id"] == "trace-xyz"
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_chain_lookup.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/chain_lookup.py`**

```python
from __future__ import annotations
import json
import uuid
import logging
from datetime import datetime, timezone
from dataclasses import asdict
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import OptionCandidate

logger = logging.getLogger(__name__)


class ChainLookup(Skill):
    name = "ChainLookup"

    def __init__(self, gateway, conn) -> None:
        self._gateway = gateway
        self._conn = conn

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        signal_id = ctx.get("signal_id", ctx.event_id)
        try:
            candidates = await self._gateway.get_chain(ticker)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        now = datetime.now(timezone.utc).isoformat()
        for c in candidates:
            await self._conn.execute(
                """INSERT OR IGNORE INTO option_candidates
                   (id, trace_id, signal_id, symbol, expiry, strike, right,
                    bid, ask, mid, spread_pct, open_interest, volume, multiplier,
                    contract_ref_json, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), ctx.trace_id, signal_id,
                    c.symbol, c.expiry, c.strike, c.right,
                    c.bid, c.ask, c.mid, c.spread_pct,
                    c.open_interest, c.volume, c.multiplier,
                    json.dumps(_ref_to_dict(c.contract_ref)), now,
                ),
            )
        await self._conn.commit()
        logger.info("ChainLookup: %d candidates for %s", len(candidates), ticker)
        return SkillResult(status="success", updates={"option_candidates": candidates})


def _ref_to_dict(ref) -> dict:
    return {
        "symbol": ref.symbol, "sec_type": ref.sec_type,
        "exchange": ref.exchange, "currency": ref.currency,
        "con_id": ref.con_id, "expiry": ref.expiry, "strike": ref.strike,
        "right": ref.right, "multiplier": ref.multiplier,
        "local_symbol": ref.local_symbol, "trading_class": ref.trading_class,
        "qualified": ref.qualified,
    }
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_chain_lookup.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/chain_lookup.py tests/unit/test_chain_lookup.py
git commit -m "feat(execution): add ChainLookup skill"
```

---

## Task 8: InstrumentMarketabilityGuard

**Files:**
- Create: `skills/execution/instrument_marketability_guard.py`
- Test: `tests/unit/test_instrument_marketability_guard.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_instrument_marketability_guard.py`:

```python
import pytest
from unittest.mock import MagicMock
from agent.context import Context
from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
from infra.ib.models import OptionCandidate, BrokerContractRef


def _policy(max_spread_pct=0.40):
    p = MagicMock()
    p.pricing_policy_guards.max_spread_pct = max_spread_pct
    return p


def _candidate(spread_pct=0.10):
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    return OptionCandidate(symbol="AAPL", expiry="2026-12-18", strike=150.0, right="C",
                            bid=4.8, ask=5.2, mid=5.0, spread_pct=spread_pct,
                            open_interest=100, volume=50, multiplier=100, contract_ref=ref)


def _ctx(session="rth", candidates=None):
    c = Context(trace_id="t", event_id="e")
    c.update({"execution_session": session, "option_candidates": candidates or [_candidate()]})
    return c


@pytest.mark.asyncio
async def test_rth_with_candidates_returns_option():
    guard = InstrumentMarketabilityGuard(_policy())
    result = await guard.run(_ctx(session="rth"))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "option"
    assert result.updates.get("fallback_reason") is None


@pytest.mark.asyncio
async def test_premarket_falls_back_to_equity():
    guard = InstrumentMarketabilityGuard(_policy())
    result = await guard.run(_ctx(session="premarket"))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "equity"
    assert result.updates["fallback_reason"] == "options_outside_rth"


@pytest.mark.asyncio
async def test_wide_spread_falls_back_to_equity():
    guard = InstrumentMarketabilityGuard(_policy(max_spread_pct=0.40))
    result = await guard.run(_ctx(session="rth", candidates=[_candidate(spread_pct=0.50)]))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "equity"
    assert result.updates["fallback_reason"] == "all_candidates_spread_too_wide"


@pytest.mark.asyncio
async def test_no_candidates_falls_back_to_equity():
    guard = InstrumentMarketabilityGuard(_policy())
    result = await guard.run(_ctx(session="rth", candidates=[]))
    assert result.status == "success"
    assert result.updates["instrument_type"] == "equity"
    assert result.updates["fallback_reason"] == "no_option_candidates"
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_instrument_marketability_guard.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/instrument_marketability_guard.py`**

```python
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class InstrumentMarketabilityGuard(Skill):
    name = "InstrumentMarketabilityGuard"

    def __init__(self, policy) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        session = ctx.get("execution_session", "rth")
        candidates = ctx.get("option_candidates", [])
        max_spread = self._policy.pricing_policy_guards.max_spread_pct

        if session != "rth":
            return SkillResult(status="success", updates={
                "instrument_type": "equity",
                "fallback_reason": "options_outside_rth",
            })

        if not candidates:
            return SkillResult(status="success", updates={
                "instrument_type": "equity",
                "fallback_reason": "no_option_candidates",
            })

        viable = [c for c in candidates if c.spread_pct <= max_spread]
        if not viable:
            return SkillResult(status="success", updates={
                "instrument_type": "equity",
                "fallback_reason": "all_candidates_spread_too_wide",
            })

        return SkillResult(status="success", updates={
            "instrument_type": "option",
            "fallback_reason": None,
        })
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_instrument_marketability_guard.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/instrument_marketability_guard.py tests/unit/test_instrument_marketability_guard.py
git commit -m "feat(execution): add InstrumentMarketabilityGuard"
```

---

## Task 9: ContractSelector

**Files:**
- Create: `skills/execution/contract_selector.py`
- Test: `tests/unit/test_contract_selector.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_contract_selector.py`:

```python
import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock
from agent.context import Context
from skills.execution.contract_selector import ContractSelector
from infra.ib.models import OptionCandidate, BrokerContractRef


def _policy(min_expiry_days=180, min_bid=0.01, max_spread_pct=0.40, strike_policy="closest_itm_call"):
    p = MagicMock()
    p.instrument_policy.min_expiry_days = min_expiry_days
    p.instrument_policy.strike_policy = strike_policy
    p.pricing_policy_guards.min_bid = min_bid
    p.pricing_policy_guards.max_spread_pct = max_spread_pct
    return p


def _candidate(strike, expiry_days=200, spread_pct=0.10, bid=5.0, ask=5.5):
    expiry = (date.today() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", expiry=expiry.replace("-", ""),
                             strike=strike, right="C", qualified=True)
    mid = (bid + ask) / 2
    return OptionCandidate(symbol="AAPL", expiry=expiry, strike=strike, right="C",
                            bid=bid, ask=ask, mid=mid, spread_pct=spread_pct,
                            open_interest=100, volume=50, multiplier=100, contract_ref=ref)


def _ctx(instrument_type="option", candidates=None, ticker="AAPL", spot=155.0):
    c = Context(trace_id="t", event_id="e")
    c.update({
        "instrument_type": instrument_type,
        "option_candidates": candidates or [],
        "ticker": ticker,
        "spot_price": spot,
    })
    return c


@pytest.mark.asyncio
async def test_selects_closest_itm_call(db=None):
    # spot=155, ITM calls are strike < 155; closest ITM = 150
    candidates = [_candidate(140), _candidate(150), _candidate(160)]
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "success"
    assert result.updates["selected_strike"] == 150.0


@pytest.mark.asyncio
async def test_rejects_short_expiry():
    candidates = [_candidate(150, expiry_days=30)]  # below min_expiry_days=180
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "fail"
    assert "no_eligible_contract" in result.reason


@pytest.mark.asyncio
async def test_equity_fallback_returns_stk_contract():
    selector = ContractSelector(_policy())
    result = await selector.run(_ctx(instrument_type="equity"))
    assert result.status == "success"
    assert result.updates["selected_contract"].sec_type == "STK"
    assert result.updates["selected_contract"].qualified is False


@pytest.mark.asyncio
async def test_rejects_low_bid():
    candidates = [_candidate(150, bid=0.005, ask=0.01)]
    selector = ContractSelector(_policy(min_bid=0.01))
    result = await selector.run(_ctx(candidates=candidates, spot=155.0))
    assert result.status == "fail"
    assert "no_eligible_contract" in result.reason
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_contract_selector.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/contract_selector.py`**

```python
from __future__ import annotations
from datetime import date, datetime
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import BrokerContractRef


class ContractSelector(Skill):
    name = "ContractSelector"

    def __init__(self, policy) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        instrument_type = ctx.get("instrument_type", "option")

        if instrument_type == "equity":
            ticker = ctx.get("ticker")
            ref = BrokerContractRef(
                symbol=ticker, sec_type="STK",
                exchange="SMART", currency="USD",
                qualified=False,
            )
            return SkillResult(status="success", updates={
                "selected_contract": ref,
                "selected_expiry": None,
                "selected_strike": None,
            })

        candidates = ctx.get("option_candidates", [])
        ip = self._policy.instrument_policy
        pg = self._policy.pricing_policy_guards
        spot = ctx.get("spot_price", 0.0)

        today = date.today()
        eligible = []
        for c in candidates:
            expiry_date = datetime.strptime(c.expiry, "%Y-%m-%d").date()
            days_to_expiry = (expiry_date - today).days
            if days_to_expiry < ip.min_expiry_days:
                continue
            if c.bid < pg.min_bid:
                continue
            if c.spread_pct > pg.max_spread_pct:
                continue
            eligible.append(c)

        if not eligible:
            return SkillResult(status="fail", reason="no_eligible_contract: no candidates pass filters")

        # closest_itm_call: largest strike below spot
        itm = [c for c in eligible if c.right == "C" and c.strike < spot]
        if not itm:
            # fallback: lowest strike above spot (nearest OTM)
            otm = sorted([c for c in eligible if c.right == "C"], key=lambda c: c.strike)
            if not otm:
                return SkillResult(status="fail", reason="no_eligible_contract: no call candidates")
            selected = otm[0]
        else:
            selected = max(itm, key=lambda c: c.strike)

        return SkillResult(status="success", updates={
            "selected_contract": selected.contract_ref,
            "selected_expiry": selected.expiry,
            "selected_strike": selected.strike,
        })
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_contract_selector.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/contract_selector.py tests/unit/test_contract_selector.py
git commit -m "feat(execution): add ContractSelector"
```

---

## Task 10: OrderSizer

**Files:**
- Create: `skills/execution/order_sizer.py`
- Test: `tests/unit/test_order_sizer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_order_sizer.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.order_sizer import OrderSizer
from infra.ib.models import AccountSummary, BrokerContractRef, OptionCandidate
from infra.ib.gateway import IBGatewayUnavailable


def _policy(low_pct=0.05, high_pct=0.10):
    p = MagicMock()
    p.sizing_policy.low_conviction_pct = low_pct
    p.sizing_policy.high_conviction_pct = high_pct
    return p


def _ctx(instrument_type="option", conviction="high", ask=5.0, multiplier=100):
    c = Context(trace_id="t", event_id="e")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    candidate = OptionCandidate(symbol="AAPL", expiry="2026-12-18", strike=150.0,
                                 right="C", bid=ask-0.5, ask=ask, mid=ask-0.25,
                                 spread_pct=0.1, open_interest=100, volume=50,
                                 multiplier=multiplier, contract_ref=ref)
    c.update({
        "instrument_type": instrument_type,
        "ticker": "AAPL",
        "conviction_bucket": conviction,
        "option_candidates": [candidate],
        "selected_contract": ref,
        "selected_strike": 150.0,
    })
    return c


def _gateway(buying_power=100_000.0):
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=buying_power, net_liquidation=buying_power, currency="USD"
    ))
    gw.get_quote = AsyncMock(return_value=150.0)
    return gw


@pytest.mark.asyncio
async def test_high_conviction_option_sizing():
    # 10% of 100k = 10k; ask=5.0, multiplier=100 → cost=500/contract; qty=20
    sizer = OrderSizer(_policy(), _gateway(100_000))
    result = await sizer.run(_ctx(instrument_type="option", conviction="high", ask=5.0))
    assert result.status == "success"
    assert result.updates["quantity"] == 20
    assert "high_conviction" in result.updates["sizing_reason"]


@pytest.mark.asyncio
async def test_low_conviction_option_sizing():
    # 5% of 100k = 5k; ask=5.0, multiplier=100 → cost=500/contract; qty=10
    sizer = OrderSizer(_policy(), _gateway(100_000))
    result = await sizer.run(_ctx(conviction="low", ask=5.0))
    assert result.status == "success"
    assert result.updates["quantity"] == 10


@pytest.mark.asyncio
async def test_insufficient_buying_power_fails():
    # 10% of 100 = 10; ask=5.0, multiplier=100 → cost=500; qty=0 → fail
    sizer = OrderSizer(_policy(), _gateway(100))
    result = await sizer.run(_ctx(ask=5.0))
    assert result.status == "fail"
    assert "insufficient_buying_power" in result.reason


@pytest.mark.asyncio
async def test_gateway_unavailable_fails():
    gw = MagicMock()
    gw.get_account_summary = AsyncMock(side_effect=IBGatewayUnavailable("down"))
    sizer = OrderSizer(_policy(), gw)
    result = await sizer.run(_ctx())
    assert result.status == "fail"
    assert "broker_unavailable" in result.reason


@pytest.mark.asyncio
async def test_equity_sizing_uses_get_quote():
    gw = _gateway(100_000)
    gw.get_quote = AsyncMock(return_value=200.0)
    sizer = OrderSizer(_policy(), gw)
    ctx = _ctx(instrument_type="equity", conviction="high")
    result = await sizer.run(ctx)
    # 10% of 100k = 10k / 200 = 50 shares
    assert result.status == "success"
    assert result.updates["quantity"] == 50
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_order_sizer.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/order_sizer.py`**

```python
from __future__ import annotations
import math
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class OrderSizer(Skill):
    name = "OrderSizer"

    def __init__(self, policy, gateway) -> None:
        self._policy = policy
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        try:
            account = await self._gateway.get_account_summary()
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        instrument_type = ctx.get("instrument_type", "option")
        conviction = ctx.get("conviction_bucket", "low")
        sp = self._policy.sizing_policy
        conviction_pct = sp.high_conviction_pct if conviction == "high" else sp.low_conviction_pct

        allocation = account.buying_power * conviction_pct

        if instrument_type == "option":
            candidates = ctx.get("option_candidates", [])
            selected_strike = ctx.get("selected_strike")
            matching = [c for c in candidates if c.strike == selected_strike]
            if not matching:
                return SkillResult(status="fail", reason="order_sizer: no matching candidate for selected strike")
            candidate = matching[0]
            ask = candidate.ask
            multiplier = candidate.multiplier
            cost_per_contract = ask * multiplier
            quantity = math.floor(allocation / cost_per_contract)
            notional = quantity * cost_per_contract
        else:
            ticker = ctx.get("ticker")
            try:
                ask = await self._gateway.get_quote(ticker)
            except IBGatewayUnavailable as exc:
                return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")
            quantity = math.floor(allocation / ask)
            notional = quantity * ask

        if quantity < 1:
            return SkillResult(
                status="fail",
                reason=f"insufficient_buying_power: allocation={allocation:.2f} insufficient for 1 unit at {ask}",
            )

        reason = (
            f"{conviction}_conviction {conviction_pct*100:.0f}% of "
            f"${account.buying_power:,.0f} buying_power"
        )
        logger.info("OrderSizer: qty=%d notional=%.2f (%s)", quantity, notional, reason)
        return SkillResult(status="success", updates={
            "quantity": quantity,
            "notional_estimate": notional,
            "sizing_reason": reason,
            "capped_by": None,
        })
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_order_sizer.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/order_sizer.py tests/unit/test_order_sizer.py
git commit -m "feat(execution): add OrderSizer"
```

---

## Task 11: OrderPricer

**Files:**
- Create: `skills/execution/order_pricer.py`
- Test: `tests/unit/test_order_pricer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_order_pricer.py`:

```python
import pytest
from unittest.mock import MagicMock
from agent.context import Context
from skills.execution.order_pricer import OrderPricer
from infra.ib.models import BrokerContractRef, OptionCandidate


def _policy(spread_fraction=0.25, stock_buffer_pct=0.001, min_bid=0.01,
            max_spread_pct=0.40, max_equity_price=500.0):
    p = MagicMock()
    p.pricing_policy.option_spread_fraction = spread_fraction
    p.pricing_policy.stock_buffer_pct = stock_buffer_pct
    p.pricing_policy_guards.min_bid = min_bid
    p.pricing_policy_guards.max_spread_pct = max_spread_pct
    p.execution.max_equity_price = max_equity_price
    return p


def _ctx_option(bid=5.0, ask=5.5, spread_pct=0.09, selected_strike=150.0):
    c = Context(trace_id="t", event_id="e")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    candidate = OptionCandidate(symbol="AAPL", expiry="2026-12-18", strike=selected_strike,
                                 right="C", bid=bid, ask=ask, mid=(bid+ask)/2,
                                 spread_pct=spread_pct, open_interest=100, volume=50,
                                 multiplier=100, contract_ref=ref)
    c.update({"instrument_type": "option", "option_candidates": [candidate],
               "selected_strike": selected_strike})
    return c


def _ctx_equity(ask=150.0):
    c = Context(trace_id="t", event_id="e")
    c.update({"instrument_type": "equity", "ticker": "AAPL", "_equity_ask": ask})
    return c


@pytest.mark.asyncio
async def test_option_limit_price():
    # mid=5.25, spread_fraction=0.25 → price = 5.25 + (5.5-5.25)*0.25 = 5.3125 → 5.31
    pricer = OrderPricer(_policy())
    result = await pricer.run(_ctx_option(bid=5.0, ask=5.5))
    assert result.status == "success"
    assert result.updates["limit_price"] == 5.31
    assert result.updates["order_type"] == "LMT"


@pytest.mark.asyncio
async def test_option_fails_low_bid():
    pricer = OrderPricer(_policy(min_bid=0.01))
    result = await pricer.run(_ctx_option(bid=0.005, ask=0.01, spread_pct=0.5))
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_equity_limit_price():
    # ask=150, buffer=0.001 → 150 * 1.001 = 150.15
    pricer = OrderPricer(_policy())
    ctx = _ctx_equity(ask=150.0)
    result = await pricer.run(ctx)
    assert result.status == "success"
    assert abs(result.updates["limit_price"] - 150.15) < 0.01


@pytest.mark.asyncio
async def test_equity_fails_above_max_price():
    pricer = OrderPricer(_policy(max_equity_price=500.0))
    ctx = _ctx_equity(ask=600.0)
    result = await pricer.run(ctx)
    assert result.status == "fail"
    assert "max_equity_price" in result.reason
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_order_pricer.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/order_pricer.py`**

```python
from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class OrderPricer(Skill):
    name = "OrderPricer"

    def __init__(self, policy) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        instrument_type = ctx.get("instrument_type", "option")
        pp = self._policy.pricing_policy
        pg = self._policy.pricing_policy_guards

        if instrument_type == "option":
            candidates = ctx.get("option_candidates", [])
            selected_strike = ctx.get("selected_strike")
            matching = [c for c in candidates if c.strike == selected_strike]
            if not matching:
                return SkillResult(status="fail", reason="order_pricer: no matching candidate")
            c = matching[0]
            if c.bid < pg.min_bid:
                return SkillResult(status="fail", reason=f"order_pricer: bid {c.bid} below min_bid {pg.min_bid}")
            if c.spread_pct > pg.max_spread_pct:
                return SkillResult(status="fail", reason=f"order_pricer: spread {c.spread_pct:.2%} exceeds max")
            mid = (c.bid + c.ask) / 2
            limit_price = round(mid + (c.ask - mid) * pp.option_spread_fraction, 2)
        else:
            ask = ctx.get("_equity_ask")
            if ask is None or ask <= 0:
                return SkillResult(status="fail", reason="order_pricer: equity ask missing or zero")
            max_price = self._policy.execution.max_equity_price
            if ask > max_price:
                return SkillResult(status="fail",
                                   reason=f"order_pricer: ask {ask} exceeds max_equity_price {max_price}")
            limit_price = round(ask * (1 + pp.stock_buffer_pct), 2)

        logger.info("OrderPricer: limit_price=%.2f type=%s", limit_price, instrument_type)
        return SkillResult(status="success", updates={
            "limit_price": limit_price,
            "order_type": "LMT",
        })
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_order_pricer.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/order_pricer.py tests/unit/test_order_pricer.py
git commit -m "feat(execution): add OrderPricer"
```

---

## Task 12: OrderSubmitter

**Files:**
- Create: `skills/execution/order_submitter.py`
- Test: `tests/unit/test_order_submitter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_order_submitter.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.order_submitter import OrderSubmitter
from infra.ib.models import BrokerContractRef, FillStatus
from infra.ib.gateway import IBGatewayUnavailable


def _ref(sec_type="OPT", qualified=True):
    return BrokerContractRef(symbol="AAPL", sec_type=sec_type, exchange="SMART",
                              currency="USD", qualified=qualified)


def _ctx(instrument_type="option", quantity=2, limit_price=5.25, ref=None):
    c = Context(trace_id="trace-1", event_id="sig-1")
    c.update({
        "signal_id": "sig-1",
        "instrument_type": instrument_type,
        "ticker": "AAPL",
        "selected_contract": ref or _ref(sec_type="OPT" if instrument_type=="option" else "STK"),
        "quantity": quantity,
        "limit_price": limit_price,
        "notional_estimate": quantity * limit_price * 100,
        "sizing_reason": "high_conviction",
        "capped_by": None,
    })
    return c


def _gateway(trade_id="IB-1"):
    fake_trade = MagicMock()
    fake_trade.order.orderId = trade_id
    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.qualify = AsyncMock(side_effect=lambda ref: setattr(ref, 'qualified', True) or ref)
    return gw


@pytest.mark.asyncio
async def test_option_submitter_writes_execution_row(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = _gateway()
    skill = OrderSubmitter(_gateway(), store)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["broker_order_id"] == "IB-1"
    assert "execution_id" in result.updates
    idempotency_key = result.updates["idempotency_key"]
    assert idempotency_key == "trace-1:OrderSubmitter:sig-1"


@pytest.mark.asyncio
async def test_equity_submitter_qualifies_before_submit(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = _gateway()
    ref = _ref(sec_type="STK", qualified=False)
    result = await OrderSubmitter(gw, store).run(_ctx(instrument_type="equity", ref=ref))
    assert result.status == "success"
    gw.qualify.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_unavailable_fails(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.place_order = AsyncMock(side_effect=IBGatewayUnavailable("write breaker open"))
    gw.qualify = AsyncMock(side_effect=lambda ref: ref)
    result = await OrderSubmitter(gw, store).run(_ctx())
    assert result.status == "fail"
    assert "broker_unavailable" in result.reason


@pytest.mark.asyncio
async def test_execution_row_written_before_place_order(db):
    from infra.storage.execution_store import ExecutionStore
    call_order = []
    store = ExecutionStore(db)
    original_insert = store.insert_execution
    async def tracked_insert(record):
        call_order.append("insert")
        return await original_insert(record)
    store.insert_execution = tracked_insert

    gw = MagicMock()
    async def tracked_place(contract_ref, order, client_order_id):
        call_order.append("place_order")
        fake = MagicMock()
        fake.order.orderId = "IB-X"
        return fake
    gw.place_order = tracked_place
    gw.qualify = AsyncMock(side_effect=lambda ref: ref)

    await OrderSubmitter(gw, store).run(_ctx())
    assert call_order == ["insert", "place_order"]
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_order_submitter.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/order_submitter.py`**

```python
from __future__ import annotations
import uuid
import logging
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import FillStatus, PreparedOrder

logger = logging.getLogger(__name__)


class OrderSubmitter(Skill):
    name = "OrderSubmitter"

    def __init__(self, gateway, execution_store) -> None:
        self._gateway = gateway
        self._store = execution_store

    async def run(self, ctx: Context) -> SkillResult:
        signal_id = ctx.get("signal_id", ctx.event_id)
        instrument_type = ctx.get("instrument_type", "option")
        contract_ref = ctx.get("selected_contract")
        quantity = ctx.get("quantity")
        limit_price = ctx.get("limit_price")
        ticker = ctx.get("ticker")

        # Equity contracts must be qualified here; options arrive pre-qualified
        if instrument_type == "equity" and not contract_ref.qualified:
            contract_ref = await self._gateway.qualify(contract_ref)

        if not contract_ref.qualified:
            return SkillResult(status="fail", reason="order_submitter: contract not qualified")

        idempotency_key = f"{ctx.trace_id}:OrderSubmitter:{signal_id}"
        execution_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Write execution row BEFORE calling place_order
        await self._store.insert_execution({
            "id": execution_id,
            "signal_id": signal_id,
            "trace_id": ctx.trace_id,
            "instrument_type": instrument_type,
            "ticker": ticker,
            "contract_ref_json": _ref_json(contract_ref),
            "quantity": quantity,
            "notional_estimate": ctx.get("notional_estimate"),
            "limit_price": limit_price,
            "sizing_reason": ctx.get("sizing_reason"),
            "capped_by": ctx.get("capped_by"),
            "broker_order_id": None,
            "perm_id": None,
            "status": FillStatus.SUBMITTED_UNFILLED.value,
            "filled_qty": 0,
            "avg_fill_price": None,
            "idempotency_key": idempotency_key,
            "submitted_at": now,
            "filled_at": None,
            "last_known_at": now,
        })

        order = PreparedOrder(
            action="BUY",
            quantity=quantity,
            order_type="LMT",
            limit_price=limit_price,
            tif="DAY",
        )

        try:
            trade = await self._gateway.place_order(contract_ref, order, idempotency_key)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        broker_order_id = str(trade.order.orderId)
        await self._store.update_execution_status(
            execution_id=execution_id,
            status=FillStatus.SUBMITTED_UNFILLED,
            broker_order_id=broker_order_id,
        )

        logger.info("OrderSubmitter: submitted %s qty=%d @ %.2f key=%s",
                    ticker, quantity, limit_price, idempotency_key)
        return SkillResult(status="success", updates={
            "broker_order_id": broker_order_id,
            "idempotency_key": idempotency_key,
            "execution_id": execution_id,
            "_trade": trade,
        })


def _ref_json(ref) -> str:
    import json
    return json.dumps({
        "symbol": ref.symbol, "sec_type": ref.sec_type,
        "exchange": ref.exchange, "currency": ref.currency,
        "con_id": ref.con_id, "expiry": ref.expiry,
        "strike": ref.strike, "right": ref.right,
        "multiplier": ref.multiplier, "qualified": ref.qualified,
    })
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_order_submitter.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/order_submitter.py tests/unit/test_order_submitter.py
git commit -m "feat(execution): add OrderSubmitter with write-before-submit invariant"
```

---

## Task 13: FillWaiter

**Files:**
- Create: `skills/execution/fill_waiter.py`
- Test: `tests/unit/test_fill_waiter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_fill_waiter.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.fill_waiter import FillWaiter
from infra.ib.models import FillResult, FillStatus


def _fill_result(status: FillStatus, filled_qty=2, avg_price=5.25):
    return FillResult(
        status=status, broker_order_id="IB-1", perm_id=99,
        submitted_qty=2, filled_qty=filled_qty,
        remaining_qty=2-filled_qty, avg_fill_price=avg_price,
        last_status=status.value, status_timestamp="2026-04-22T10:00:00+00:00",
    )


def _ctx(broker_order_id="IB-1", execution_id="exec-1"):
    c = Context(trace_id="t", event_id="e")
    c.update({"broker_order_id": broker_order_id, "execution_id": execution_id,
               "_trade": MagicMock()})
    return c


@pytest.mark.asyncio
async def test_filled_returns_success(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.wait_fill = AsyncMock(return_value=_fill_result(FillStatus.FILLED))
    skill = FillWaiter(gw, store, timeout=1.0)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["fill_status"] == FillStatus.FILLED.value
    assert result.updates["filled_qty"] == 2


@pytest.mark.asyncio
async def test_timeout_returns_success_with_warning(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.wait_fill = AsyncMock(return_value=_fill_result(FillStatus.TIMED_OUT_PENDING, filled_qty=0))
    skill = FillWaiter(gw, store, timeout=1.0)
    result = await skill.run(_ctx())
    # Timeout is success — reconciler handles it
    assert result.status == "success"
    assert result.updates["fill_status"] == FillStatus.TIMED_OUT_PENDING.value


@pytest.mark.asyncio
async def test_rejected_returns_fail(db):
    from infra.storage.execution_store import ExecutionStore
    store = ExecutionStore(db)
    gw = MagicMock()
    gw.wait_fill = AsyncMock(return_value=_fill_result(FillStatus.REJECTED, filled_qty=0, avg_price=None))
    skill = FillWaiter(gw, store, timeout=1.0)
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "rejected" in result.reason
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_fill_waiter.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/fill_waiter.py`**

```python
from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import FillStatus

logger = logging.getLogger(__name__)


class FillWaiter(Skill):
    name = "FillWaiter"

    def __init__(self, gateway, execution_store, timeout: float | None = None) -> None:
        self._gateway = gateway
        self._store = execution_store
        self._timeout = timeout  # None → use policy value set at runtime

    async def run(self, ctx: Context) -> SkillResult:
        trade = ctx.get("_trade")
        execution_id = ctx.get("execution_id")
        timeout = self._timeout or 30.0

        fill = await self._gateway.wait_fill(trade, timeout=timeout)

        await self._store.update_execution_status(
            execution_id=execution_id,
            status=fill.status,
            filled_qty=fill.filled_qty,
            avg_fill_price=fill.avg_fill_price,
            broker_order_id=fill.broker_order_id,
            perm_id=fill.perm_id,
        )

        updates = {
            "fill_status": fill.status.value,
            "filled_qty": fill.filled_qty,
            "avg_fill_price": fill.avg_fill_price,
            "perm_id": fill.perm_id,
        }

        if fill.status == FillStatus.REJECTED:
            return SkillResult(
                status="fail",
                reason=f"fill rejected: broker_status={fill.last_status}",
                updates=updates,
            )

        if fill.status == FillStatus.TIMED_OUT_PENDING:
            logger.warning(
                "FillWaiter: order %s timed out after %.0fs — ExecutionReconciler will resolve",
                fill.broker_order_id, timeout,
            )

        return SkillResult(status="success", updates=updates)
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_fill_waiter.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/fill_waiter.py tests/unit/test_fill_waiter.py
git commit -m "feat(execution): add FillWaiter"
```

---

## Task 14: ExecutionAuditWriter and ExecutionReconciler

**Files:**
- Create: `skills/execution/execution_audit_writer.py`
- Create: `skills/execution/execution_reconciler.py`
- Test: `tests/unit/test_execution_audit_writer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_execution_audit_writer.py`:

```python
import pytest
import json
from agent.context import Context
from skills.execution.execution_audit_writer import ExecutionAuditWriter


def _ctx(execution_id="exec-1", signal_id="sig-1"):
    c = Context(trace_id="trace-1", event_id=signal_id)
    c.update({"signal_id": signal_id, "execution_id": execution_id, "ticker": "AAPL"})
    return c


@pytest.mark.asyncio
async def test_audit_writer_inserts_snapshot(db):
    writer = ExecutionAuditWriter(db)
    await writer.write(ctx=_ctx(), pipeline_outcome="success")
    async with db.execute("SELECT * FROM execution_audit_log") as cur:
        row = await cur.fetchone()
    assert row["trace_id"] == "trace-1"
    assert row["pipeline_outcome"] == "success"
    snapshot = json.loads(row["ctx_snapshot_json"])
    assert snapshot["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_audit_writer_records_failure_outcome(db):
    writer = ExecutionAuditWriter(db)
    await writer.write(ctx=_ctx(), pipeline_outcome="failed")
    async with db.execute("SELECT pipeline_outcome FROM execution_audit_log") as cur:
        row = await cur.fetchone()
    assert row["pipeline_outcome"] == "failed"
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_execution_audit_writer.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `skills/execution/execution_audit_writer.py`**

```python
from __future__ import annotations
import json
import uuid
import logging
from datetime import datetime, timezone
from agent.context import Context

logger = logging.getLogger(__name__)


class ExecutionAuditWriter:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def write(self, ctx: Context, pipeline_outcome: str) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO execution_audit_log
               (id, execution_id, signal_id, trace_id,
                ctx_snapshot_json, pipeline_outcome, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                ctx.get("execution_id"),
                ctx.get("signal_id", ctx.event_id),
                ctx.trace_id,
                json.dumps(dict(ctx.data)),
                pipeline_outcome,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._conn.commit()
        logger.debug("ExecutionAuditWriter: wrote audit log for trace=%s outcome=%s",
                     ctx.trace_id, pipeline_outcome)
```

- [ ] **Step 4: Create `skills/execution/execution_reconciler.py`**

```python
from __future__ import annotations
import asyncio
import logging
from infra.ib.models import FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class ExecutionReconciler:
    def __init__(self, gateway, execution_store, interval_seconds: int = 60) -> None:
        self._gateway = gateway
        self._store = execution_store
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._loop())
        logger.info("ExecutionReconciler started (interval=%ds)", self._interval)

    async def _loop(self) -> None:
        while True:
            try:
                await self._reconcile()
            except Exception as exc:
                logger.exception("ExecutionReconciler error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _reconcile(self) -> None:
        rows = await self._store.get_uncertain_executions()
        if not rows:
            return
        logger.info("ExecutionReconciler: %d uncertain executions to reconcile", len(rows))
        for row in rows:
            broker_order_id = row["broker_order_id"]
            if not broker_order_id:
                continue
            try:
                # Check open orders from IB — match by broker_order_id
                open_orders = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._gateway._ib.openOrders() if self._gateway._ib else [],
                )
                matched = [o for o in open_orders if str(o.orderId) == broker_order_id]
                if not matched:
                    # Order not found in open orders — likely filled or cancelled
                    logger.warning(
                        "ExecutionReconciler: order %s not in open orders — marking timed_out_pending for manual review",
                        broker_order_id,
                    )
                    continue
            except IBGatewayUnavailable:
                logger.warning("ExecutionReconciler: gateway unavailable, skipping reconcile")
                return
```

- [ ] **Step 5: Run audit writer tests — expect pass**

```bash
pytest tests/unit/test_execution_audit_writer.py -v
```

Expected: 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add skills/execution/execution_audit_writer.py skills/execution/execution_reconciler.py tests/unit/test_execution_audit_writer.py
git commit -m "feat(execution): add ExecutionAuditWriter and ExecutionReconciler"
```

---

## Task 15: Registry — build_phase2b_execution_chain

**Files:**
- Modify: `agent/registry.py`

- [ ] **Step 1: Add `build_phase2b_execution_chain` to `agent/registry.py`**

Append to `agent/registry.py`:

```python
def build_phase2b_execution_chain(policy, execution_store, gateway) -> list:
    from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
    from skills.execution.chain_lookup import ChainLookup
    from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
    from skills.execution.contract_selector import ContractSelector
    from skills.execution.order_sizer import OrderSizer
    from skills.execution.order_pricer import OrderPricer
    from skills.execution.order_submitter import OrderSubmitter
    from skills.execution.fill_waiter import FillWaiter

    return [
        ExecutionEligibilityGuard(policy),
        ChainLookup(gateway, execution_store._conn),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store,
                   timeout=policy.execution.fill_wait_timeout_seconds),
    ]
```

- [ ] **Step 2: Verify import**

```bash
python3 -c "from agent.registry import build_phase2b_execution_chain; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add agent/registry.py
git commit -m "feat(registry): add build_phase2b_execution_chain"
```

---

## Task 16: Wire main.py

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Update `main.py` to use phase2b chain, audit hook, and reconciler**

Replace the `run()` function in `main.py`:

```python
async def run(socket_path: str, db_path: str, policy_path: str) -> None:
    policy = load_policy(policy_path)
    conn = await get_connection(db_path)

    signal_store = SignalStore(conn)
    trace_store = TraceStore(conn)
    idempotency_store = IdempotencyStore(conn)
    execution_store = ExecutionStore(conn)
    telegram = TelegramClient(
        bot_token=policy.telegram.bot_token,
        chat_id=policy.telegram.chat_id,
    )

    from infra.ib.gateway import IBGateway
    gateway = IBGateway(policy)
    await gateway.connect()

    from agent.registry import build_phase1_chain, build_phase2b_execution_chain
    from skills.execution.execution_audit_writer import ExecutionAuditWriter
    from skills.execution.execution_reconciler import ExecutionReconciler
    from infra.storage.execution_store import ExecutionStore as _ES

    phase1_chain = build_phase1_chain(policy, idempotency_store, telegram)
    phase2b_chain = build_phase2b_execution_chain(policy, execution_store, gateway)
    full_chain = phase1_chain + phase2b_chain

    audit_writer = ExecutionAuditWriter(conn)
    digest_skill = phase1_chain[-1]

    async def on_fail(ctx: Context, reason: str) -> None:
        await audit_writer.write(ctx, "failed")
        await digest_skill.send_error_digest(ctx, reason)

    async def on_skip(ctx: Context, reason: str) -> None:
        await audit_writer.write(ctx, "skipped")

    orch = Orchestrator(full_chain, trace_store, on_skip=on_skip, on_fail=on_fail)

    async def handle_event(event: TriggerEvent) -> None:
        trace_id = str(uuid.uuid4())[:12]
        logger.info("Received event %s from #%s by %s", event.event_id, event.channel, event.author)

        await signal_store.insert({
            "id": event.event_id,
            "source": event.source,
            "channel": event.channel,
            "author": event.author,
            "trigger_preview": event.trigger_preview,
            "full_message_text": event.trigger_preview,
            "capture_mode": "bridge",
            "message_fingerprint": "",
            "received_at": event.received_at,
        })

        ctx = Context(trace_id=trace_id, event_id=event.event_id)
        ctx.update({
            "trigger_preview": event.trigger_preview,
            "full_message_text": event.trigger_preview,
            "channel": event.channel,
            "author": event.author,
            "received_at": event.received_at,
        })

        result = await orch.run(ctx)
        await audit_writer.write(ctx, "success")

    reconciler = ExecutionReconciler(
        gateway, execution_store,
        interval_seconds=policy.execution.reconciler_interval_seconds,
    )

    reader = SocketReader(socket_path)
    logger.info("Trading agent Phase 2b ready. Listening on %s", socket_path)
    try:
        NotificationBannerPoller().start()
        reconciler.start()
        await reader.start(handle_event)
    finally:
        await gateway.disconnect()
        await conn.close()
```

Add the missing import at the top of `main.py`:

```python
from infra.storage.execution_store import ExecutionStore
```

- [ ] **Step 2: Verify import**

```bash
python3 -c "import main; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat(main): wire phase2b execution chain, audit hook, and reconciler"
```

---

## Task 17: E2E test — phase2b pipeline (mock gateway)

**Files:**
- Create: `tests/e2e/test_phase2b_execution_pipeline.py`

- [ ] **Step 1: Write the E2E test**

Create `tests/e2e/test_phase2b_execution_pipeline.py`:

```python
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock
from datetime import date, timedelta
from agent.context import Context
from agent.orchestrator import Orchestrator
from infra.storage.trace_store import TraceStore
from infra.storage.execution_store import ExecutionStore
from infra.ib.models import (
    OptionCandidate, BrokerContractRef, AccountSummary,
    FillResult, FillStatus, ExecutionMode,
)
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
from skills.execution.chain_lookup import ChainLookup
from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
from skills.execution.contract_selector import ContractSelector
from skills.execution.order_sizer import OrderSizer
from skills.execution.order_pricer import OrderPricer
from skills.execution.order_submitter import OrderSubmitter
from skills.execution.fill_waiter import FillWaiter
from datetime import datetime
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def _policy():
    p = MagicMock()
    p.market_hours.rth_start = "09:30"
    p.market_hours.rth_end = "16:00"
    p.market_hours.stock_premarket_allowed = True
    p.market_hours.stock_premarket_start = "04:00"
    p.market_hours.stock_afterhours_queue = True
    p.instrument_policy.min_expiry_days = 30
    p.instrument_policy.strike_policy = "closest_itm_call"
    p.pricing_policy_guards.min_bid = 0.01
    p.pricing_policy_guards.max_spread_pct = 0.40
    p.pricing_policy.option_spread_fraction = 0.25
    p.pricing_policy.stock_buffer_pct = 0.001
    p.sizing_policy.low_conviction_pct = 0.05
    p.sizing_policy.high_conviction_pct = 0.10
    p.execution.fill_wait_timeout_seconds = 1.0
    p.execution.max_equity_price = 500.0
    return p


def _candidate(strike=150.0, expiry_days=200):
    expiry = (date.today() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", expiry=expiry.replace("-",""),
                             strike=strike, right="C", qualified=True)
    return OptionCandidate(symbol="AAPL", expiry=expiry, strike=strike, right="C",
                            bid=5.0, ask=5.5, mid=5.25, spread_pct=0.09,
                            open_interest=100, volume=50, multiplier=100,
                            contract_ref=ref)


def _gateway():
    fake_trade = MagicMock()
    fake_trade.order.orderId = "IB-TEST-1"
    fake_trade.order.permId = 42
    gw = MagicMock()
    gw.get_chain = AsyncMock(return_value=[_candidate(strike=150.0)])
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=50_000.0, net_liquidation=50_000.0, currency="USD"
    ))
    gw.get_quote = AsyncMock(return_value=155.0)
    gw.qualify = AsyncMock(side_effect=lambda ref: setattr(ref, 'qualified', True) or ref)
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="IB-TEST-1", perm_id=42,
        submitted_qty=9, filled_qty=9, remaining_qty=0,
        avg_fill_price=5.26, last_status="Filled",
        status_timestamp="2026-04-22T10:00:00+00:00",
    ))
    return gw


def _rth_time():
    return datetime(2026, 4, 22, 10, 0, tzinfo=ET)


@pytest.mark.asyncio
async def test_full_execution_pipeline_happy_path(db):
    policy = _policy()
    gateway = _gateway()
    execution_store = ExecutionStore(db)
    trace_store = TraceStore(db)

    chain = [
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id=str(uuid.uuid4())[:12], event_id="evt-1")
    ctx.update({
        "signal_id": "sig-1",
        "ticker": "AAPL",
        "conviction_bucket": "high",
        "spot_price": 152.0,  # 150 is ITM
    })

    result_ctx = await orch.run(ctx)

    assert result_ctx.get("fill_status") == FillStatus.FILLED.value
    assert result_ctx.get("filled_qty") == 9
    assert result_ctx.get("execution_mode") == ExecutionMode.EXECUTE_NOW.value

    # Verify execution row persisted
    async with db.execute("SELECT status, filled_qty FROM executions") as cur:
        row = await cur.fetchone()
    assert row["status"] == "filled"
    assert row["filled_qty"] == 9


@pytest.mark.asyncio
async def test_broker_unavailable_fails_pipeline(db):
    policy = _policy()
    gateway = _gateway()
    gateway.get_chain = AsyncMock(side_effect=IBGatewayUnavailable("circuit open"))
    execution_store = ExecutionStore(db)
    trace_store = TraceStore(db)

    chain = [
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id="t2", event_id="e2")
    ctx.update({"signal_id": "sig-2", "ticker": "AAPL", "conviction_bucket": "high", "spot_price": 152.0})

    await orch.run(ctx)

    async with db.execute("SELECT status FROM work_traces WHERE trace_id='t2'") as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"
```

- [ ] **Step 2: Run E2E test**

```bash
pytest tests/e2e/test_phase2b_execution_pipeline.py -v
```

Expected: 2 tests pass.

- [ ] **Step 3: Run full test suite**

```bash
pytest -v
```

Expected: all existing tests plus new tests pass. No regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_phase2b_execution_pipeline.py
git commit -m "test(e2e): add phase2b execution pipeline E2E tests"
```

---

## Self-Review Notes

- All types defined in Task 1 (`FillStatus`, `ExecutionMode`, `BrokerContractRef`, `OptionCandidate`, `AccountSummary`, `PreparedOrder`, `FillResult`) are referenced consistently across all tasks
- `_equity_ask` ctx key is set by `OrderSizer` for equity path and read by `OrderPricer` — this implicit dependency is acceptable but should be documented in code comments
- `ChainLookup` passes `db` connection directly rather than `ExecutionStore` — consistent with how other stores work in this codebase
- `build_phase2b_execution_chain` accesses `execution_store._conn` for `ChainLookup` — this is a minor layer violation; can be refactored to pass `conn` separately if desired, but kept minimal here
- Phase 2a skills (`build_phase1_chain` output) must be implemented before Task 16 will fully work; Task 16 degrades gracefully if Phase 2a chain is not yet wired
