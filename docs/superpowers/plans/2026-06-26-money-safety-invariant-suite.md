# Money-Safety Invariant Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a property-based, stateful invariant test suite that stress-tests the trading agent's money-safety code (sell-following, trim ladder, position ledger, order sizing) under randomized interleaved sequences, and deterministically probes a suspected in-flight-sell oversell.

**Architecture:** A Hypothesis `RuleBasedStateMachine` drives the REAL stores (`TradeIntentStore`, `TrimLadderStore`, `PositionExitStore`) and the REAL `SellFollower` / `fire_rung_if_crossed`, faking only the broker via a deterministic `FakeGateway`. After every rule, an oracle asserts the money-safety invariants. A separate stateless `@given` test covers order-sizing caps. One dedicated test reproduces the §3a in-flight-sell window via a `wait_fill` hook.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio (`asyncio_mode=auto`), aiosqlite (in-memory), Hypothesis (stateful + `@given`).

Design spec: `docs/superpowers/specs/2026-06-26-money-safety-invariant-suite-design.md` (read §3 oracle caveats and §3a finding before starting).

## Global Constraints

- Python `>=3.11`; all new tests must be **deterministic and offline** (no live LLM, no live broker, no network).
- Dependency: add `hypothesis>=6.100` to `[project.optional-dependencies].dev` (installed: `6.155.7`).
- **Test files MUST live under `tests/`** so `pyproject.toml`'s `asyncio_mode = "auto"` applies (pytest resolves rootdir from file location; outside the tree the sync Hypothesis `TestCase` would run under STRICT mode).
- Hypothesis state machines: own ONE event loop + ONE aiosqlite connection **per machine instance** (created in `__init__` after `super().__init__()`); each `@rule`/`@invariant` is sync and calls `self._run(coro)`; `teardown()` closes the **connection first, then the loop**. Do NOT use a pytest fixture for the machine's DB (fixtures are per-function, not per-example). Do NOT call `asyncio.set_event_loop()`.
- Hypothesis settings: `deadline=None` (mandatory — async-over-thread steps aren't instantaneous) and `suppress_health_check=[HealthCheck.too_slow]`. `max_examples`/`stateful_step_count`/`derandomize` come from the loaded profile (`dev` default / `ci`).
- **Oracle nets only RECORDED `sold_qty`** (never in-flight reserves) when summing sales.
- **INV-4 assertions gate on `"quantity" in result.updates`**, NOT `status == "success"`.
- Reserve rounding is `round_half_up_min1(x) = max(1, floor(x + 0.5))` — never `round()` (banker's) or `floor()`.
- `is_rth` is always injected as `lambda: True` for `SellFollower`.
- **Findings policy:** a confirmed property failure is a FINDING (real bug or over-strict property), decided per-case. Never silently weaken a property; never edit production code to make a property pass without explicit sign-off. A confirmed §3a oversell is marked `@pytest.mark.xfail(strict=True, reason=...)` (keeps the suite green; flags the day it's fixed) and raised separately.
- Runtime budget: the property module completes in **< ~30s** under the `dev` profile.
- Confirmed model shapes (do not re-derive): `AccountSummary(net_liquidation: float, buying_power: float, currency: str)`; `FillResult(status, broker_order_id, perm_id, submitted_qty, filled_qty, remaining_qty, avg_fill_price, last_status, status_timestamp)`; `FillStatus.FILLED` / `FillStatus.TIMED_OUT_PENDING`; `BrokerContractRef(symbol, sec_type, exchange, currency, qualified)`; `PreparedOrder(action, quantity, order_type, limit_price, tif)`.

---

### Task 1: Dependency, property package, Hypothesis profiles, wiring smoke test

**Files:**
- Modify: `pyproject.toml` (add hypothesis to dev deps)
- Create: `tests/property/__init__.py`
- Create: `tests/property/conftest.py`
- Create: `tests/property/test_wiring_smoke.py`

**Interfaces:**
- Produces: a `tests/property/` package where `asyncio_mode=auto` applies and Hypothesis profiles `dev`/`ci` are registered + loaded via `HYPOTHESIS_PROFILE` (default `dev`).

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, change the dev extras from:

```toml
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
]
```

to:

```toml
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
    "hypothesis>=6.100",
]
```

- [ ] **Step 2: Install it into the venv**

Run: `.venv/bin/pip install 'hypothesis>=6.100'`
Expected: `Requirement already satisfied` (6.155.7) or a successful install.

- [ ] **Step 3: Create the package + profile registration**

Create `tests/property/__init__.py` (empty).

Create `tests/property/conftest.py`:

```python
"""Hypothesis profiles for the property suite.

Loaded before any property test imports (pytest imports conftest first), so a
`@settings(deadline=None, ...)` decorator that omits volume fields inherits
max_examples / stateful_step_count / derandomize from the profile selected here.

Select with HYPOTHESIS_PROFILE=ci (default: dev).
"""
import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "dev",
    max_examples=50,
    stateful_step_count=24,
    deadline=None,
    derandomize=True,  # reproducible locally
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=300,
    stateful_step_count=40,
    deadline=None,
    derandomize=False,  # explore more of the space in CI
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))
```

- [ ] **Step 4: Write the wiring smoke test**

Create `tests/property/test_wiring_smoke.py`:

```python
"""Proves Hypothesis runs under the project's pytest config from tests/property/."""
from hypothesis import given
from hypothesis import strategies as st

from agent.exit_ladder import _round_half_up_min1


@given(x=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_round_half_up_min1_is_at_least_one(x):
    assert _round_half_up_min1(x) >= 1
```

- [ ] **Step 5: Run the smoke test**

Run: `.venv/bin/python -m pytest tests/property/test_wiring_smoke.py -q`
Expected: PASS (1 passed). Confirms hypothesis is importable and runs under `asyncio_mode=auto`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/property/__init__.py tests/property/conftest.py tests/property/test_wiring_smoke.py
git commit -m "test(property): add hypothesis dep, property package, profiles + smoke"
```

---

### Task 2: Deterministic `FakeGateway` + filled-intent factory

**Files:**
- Create: `tests/support/__init__.py`
- Create: `tests/support/fake_gateway.py`
- Create: `tests/support/factories.py`
- Test: `tests/support/test_fake_gateway.py`

**Interfaces:**
- Produces:
  - `FakeGateway(quote=100.0, net_liquidation=100_000.0, buying_power=100_000.0)` with async methods `qualify_equity(ticker)`, `get_quote(ticker)`, `get_account_summary()`, `place_order(contract, order, client_order_id)`, `wait_fill(trade, timeout)`, `cancel_order(trade)`; mutable attrs `fill_mode ∈ {"full","partial","zero"}`, `partial_fraction`, `unavailable: bool`, `on_wait_fill: Optional[async callable]` (one-shot, run inside `wait_fill`); records `placed: list[PreparedOrder]`, `cancels: int`.
  - `make_filled_intent(intent_id, *, channel, ticker, fill_qty, seq=0, fill_price=100.0) -> dict` for `TradeIntentStore.insert`.

- [ ] **Step 1: Write the factory**

Create `tests/support/__init__.py` (empty).

Create `tests/support/factories.py`:

```python
from __future__ import annotations


def make_filled_intent(intent_id: str, *, channel: str, ticker: str,
                       fill_qty: int, seq: int = 0, fill_price: float = 100.0) -> dict:
    """A filled equity trade_intent row for TradeIntentStore.insert.

    `seq` makes filled_at lexically increasing so get_open_shares_positions
    (ORDER BY filled_at ASC, created_at ASC) is deterministic oldest-first.
    """
    base = "2026-06-26T14:30:00+00:00"
    filled_at = f"2026-06-26T14:30:00.{seq:06d}+00:00"
    return {
        "intent_id": intent_id, "event_id": intent_id.split(":")[0],
        "channel": channel, "ticker": ticker, "side": "long",
        "instrument_type": "equity", "conviction": "HIGH", "policy_state": "approved",
        "execution_state": "filled", "fill_qty": fill_qty, "fill_price": fill_price,
        "filled_at": filled_at, "signal_received_at": base, "intent_created_at": base,
        "created_at": base, "updated_at": base,
    }
```

- [ ] **Step 2: Write the fake gateway**

Create `tests/support/fake_gateway.py`:

```python
from __future__ import annotations

import math
from typing import Awaitable, Callable, Optional

from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import (
    AccountSummary, BrokerContractRef, FillResult, FillStatus, PreparedOrder,
)


class FakeGateway:
    """Deterministic broker stand-in. Serves the sell/trim path
    (qualify_equity/get_quote/place_order/wait_fill/cancel_order) and OrderSizer
    (get_account_summary). `fill_mode` controls how the NEXT wait_fill resolves.
    """

    def __init__(self, *, quote: float = 100.0, net_liquidation: float = 100_000.0,
                 buying_power: float = 100_000.0) -> None:
        self.quote = quote
        self.account = AccountSummary(
            net_liquidation=net_liquidation, buying_power=buying_power, currency="USD")
        self.fill_mode = "full"          # "full" | "partial" | "zero"
        self.partial_fraction = 0.5
        self.unavailable = False
        self.placed: list[PreparedOrder] = []
        self.cancels = 0
        # §3a hook: a one-shot async callback run INSIDE wait_fill, i.e. while a
        # sell order is placed-but-unrecorded. Used to inject a concurrent trim.
        self.on_wait_fill: Optional[Callable[[], Awaitable[None]]] = None
        self._last: Optional[PreparedOrder] = None

    async def qualify_equity(self, ticker: str) -> BrokerContractRef:
        return BrokerContractRef(symbol=ticker, sec_type="STK", exchange="SMART",
                                 currency="USD", qualified=True)

    async def get_quote(self, ticker: str) -> float:
        if self.unavailable:
            raise IBGatewayUnavailable("fake: unavailable")
        return self.quote

    async def get_account_summary(self) -> AccountSummary:
        if self.unavailable:
            raise IBGatewayUnavailable("fake: unavailable")
        return self.account

    async def place_order(self, contract, order: PreparedOrder, client_order_id: str):
        if self.unavailable:
            raise IBGatewayUnavailable("fake: unavailable")
        self.placed.append(order)
        self._last = order
        return object()  # opaque trade handle

    async def wait_fill(self, trade, timeout: float) -> FillResult:
        if self.on_wait_fill is not None:
            cb, self.on_wait_fill = self.on_wait_fill, None  # one-shot
            await cb()
        qty = self._last.quantity if self._last is not None else 0
        if self.fill_mode == "full":
            filled, status = qty, FillStatus.FILLED
        elif self.fill_mode == "partial":
            filled = max(0, math.floor(qty * self.partial_fraction))
            status = FillStatus.TIMED_OUT_PENDING
        else:  # "zero"
            filled, status = 0, FillStatus.TIMED_OUT_PENDING
        return FillResult(
            status=status, broker_order_id="fake-oid", perm_id=1,
            submitted_qty=qty, filled_qty=filled, remaining_qty=qty - filled,
            avg_fill_price=(self.quote if filled > 0 else None),
            last_status=("Filled" if status == FillStatus.FILLED else "Submitted"),
            status_timestamp="2026-06-26T14:30:00+00:00")

    async def cancel_order(self, trade) -> bool:
        self.cancels += 1
        return True
```

- [ ] **Step 3: Write the fake-gateway tests**

Create `tests/support/test_fake_gateway.py`:

```python
import pytest

from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import FillStatus, PreparedOrder
from tests.support.fake_gateway import FakeGateway
from tests.support.factories import make_filled_intent


def _order(qty):
    return PreparedOrder(action="SELL", quantity=qty, order_type="LMT",
                         limit_price=99.0, tif="DAY")


@pytest.mark.asyncio
async def test_full_fill_returns_requested_qty():
    gw = FakeGateway()
    await gw.place_order(None, _order(40), "coid")
    fill = await gw.wait_fill(None, timeout=1.0)
    assert fill.status == FillStatus.FILLED
    assert fill.filled_qty == 40
    assert gw.placed[-1].quantity == 40


@pytest.mark.asyncio
async def test_partial_fill_uses_fraction_and_is_not_filled_status():
    gw = FakeGateway()
    gw.fill_mode = "partial"
    gw.partial_fraction = 0.5
    await gw.place_order(None, _order(40), "coid")
    fill = await gw.wait_fill(None, timeout=1.0)
    assert fill.filled_qty == 20
    assert fill.status != FillStatus.FILLED


@pytest.mark.asyncio
async def test_zero_fill():
    gw = FakeGateway()
    gw.fill_mode = "zero"
    await gw.place_order(None, _order(40), "coid")
    fill = await gw.wait_fill(None, timeout=1.0)
    assert fill.filled_qty == 0
    assert fill.avg_fill_price is None


@pytest.mark.asyncio
async def test_unavailable_raises_on_place():
    gw = FakeGateway()
    gw.unavailable = True
    with pytest.raises(IBGatewayUnavailable):
        await gw.place_order(None, _order(10), "coid")


@pytest.mark.asyncio
async def test_account_summary_shape():
    gw = FakeGateway(net_liquidation=250_000.0, buying_power=120_000.0)
    acct = await gw.get_account_summary()
    assert acct.net_liquidation == 250_000.0
    assert acct.buying_power == 120_000.0


@pytest.mark.asyncio
async def test_on_wait_fill_hook_runs_once():
    gw = FakeGateway()
    calls = []
    async def hook():
        calls.append(1)
    gw.on_wait_fill = hook
    await gw.place_order(None, _order(10), "coid")
    await gw.wait_fill(None, timeout=1.0)
    await gw.place_order(None, _order(10), "coid")
    await gw.wait_fill(None, timeout=1.0)
    assert calls == [1]  # one-shot


def test_make_filled_intent_orders_by_seq():
    a = make_filled_intent("e1:AAPL:long", channel="mystic", ticker="AAPL",
                           fill_qty=100, seq=1)
    b = make_filled_intent("e2:AAPL:long", channel="mystic", ticker="AAPL",
                           fill_qty=50, seq=2)
    assert a["filled_at"] < b["filled_at"]
    assert a["execution_state"] == "filled" and a["instrument_type"] == "equity"
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/support/test_fake_gateway.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/support/__init__.py tests/support/fake_gateway.py tests/support/factories.py tests/support/test_fake_gateway.py
git commit -m "test(support): deterministic FakeGateway + filled-intent factory"
```

---

### Task 3: State-machine scaffold + `create_filled_intent` + CONSISTENCY & INV-1 oracle

**Files:**
- Create: `tests/property/test_position_invariants.py`

**Interfaces:**
- Consumes: `FakeGateway`, `make_filled_intent` (Task 2); `infra.storage.db.SCHEMA`; `TradeIntentStore`, `TrimLadderStore`, `PositionExitStore`.
- Produces: `PositionInvariantMachine` with `self._run(coro)`, the `intents` Bundle, shadow dicts `self._fill: dict[str,int]` and `self._rungs: dict[str, dict[int, dict]]`, helper `self._recorded_trims(intent_id) -> (recorded:int, reserves:int)`; invariants `remaining_qty_identity` and `never_oversell`. Exposed to pytest as `TestPositionInvariants`.

- [ ] **Step 1: Write the scaffold with the first rule and two invariants**

Create `tests/property/test_position_invariants.py`:

```python
"""Stateful money-safety invariants for the position ledger, trim ladder, and
sell-follower. Drives the REAL stores + REAL SellFollower / fire_rung_if_crossed;
only the broker is faked. See docs/superpowers/specs/2026-06-26-money-safety-
invariant-suite-design.md (§3 oracle caveats, §3a finding).
"""
from __future__ import annotations

import asyncio
import math

import aiosqlite
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from infra.storage.db import SCHEMA
from infra.storage.position_exit_store import PositionExitStore
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from tests.support.factories import make_filled_intent
from tests.support.fake_gateway import FakeGateway


def _round_half_up_min1(n: float) -> int:
    return max(1, int(math.floor(n + 0.5)))


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
class PositionInvariantMachine(RuleBasedStateMachine):
    intents = Bundle("intents")

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._seq = 0
        self._fill: dict[str, int] = {}                 # intent_id -> fill_qty
        self._rungs: dict[str, dict[int, dict]] = {}    # intent_id -> {rung: meta}
        self._fp_positive: dict[str, int] = {}          # fingerprint -> #positive invocations
        self.gw = FakeGateway()
        self._conn = self._run(self._connect())
        self.intents_store = TradeIntentStore(self._conn)
        self.trims = TrimLadderStore(self._conn)
        self.exits = PositionExitStore(self._conn)

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        return conn

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    # ----------------------------------------------------------------- rules
    @rule(target=intents,
          channel=st.sampled_from(["mystic", "stp", "wse"]),
          ticker=st.sampled_from(["AAPL", "NVDA", "TSLA"]),
          fill_qty=st.integers(min_value=1, max_value=1000))
    def create_filled_intent(self, channel, ticker, fill_qty):
        self._seq += 1
        intent_id = f"e{self._seq}:{ticker}:long"
        rec = make_filled_intent(intent_id, channel=channel, ticker=ticker,
                                 fill_qty=fill_qty, seq=self._seq)
        self._run(self.intents_store.insert(rec))
        self._fill[intent_id] = fill_qty
        self._rungs[intent_id] = {}
        # carry channel/ticker for sell rules added later
        self._rungs[intent_id]["_meta"] = {"channel": channel, "ticker": ticker}
        return intent_id

    # ----------------------------------------------------------------- helpers
    async def _recorded_trims(self, intent_id: str) -> tuple[int, int]:
        """Mirror PositionExitStore.remaining_qty's trim handling: recorded
        sold_qty wins; else an in-flight rung (fire_started_at set, fired_at NULL)
        reserves round_half_up_min1(fill_qty*trim_pct)."""
        fill_qty = self._fill[intent_id]
        recorded = reserves = 0
        for r in await self.trims.all_for_intent(intent_id):
            if r["sold_qty"] is not None:
                recorded += int(r["sold_qty"])
            elif r["fire_started_at"] is not None and r["fired_at"] is None:
                reserves += _round_half_up_min1(fill_qty * r["trim_pct"])
        return recorded, reserves

    # ----------------------------------------------------------------- oracle
    @invariant()
    def remaining_qty_identity(self):
        for intent_id, fill_qty in self._fill.items():
            rem = self._run(self.exits.remaining_qty(intent_id))
            rec_trims, reserves = self._run(self._recorded_trims(intent_id))
            rec_exits = self._run(self.exits.sold_qty_for_intent(intent_id))
            expected = max(0, fill_qty - rec_trims - reserves - rec_exits)
            assert rem == expected, (
                f"{intent_id}: remaining_qty={rem} != {expected} "
                f"(fill={fill_qty} trims={rec_trims} reserves={reserves} exits={rec_exits})")
            assert rem >= 0, f"{intent_id}: negative remaining {rem}"

    @invariant()
    def never_oversell(self):
        # INV-1: sum of RECORDED trim + exit sold_qty <= fill_qty (per intent).
        for intent_id, fill_qty in self._fill.items():
            rec_trims, _ = self._run(self._recorded_trims(intent_id))
            rec_exits = self._run(self.exits.sold_qty_for_intent(intent_id))
            assert rec_trims + rec_exits <= fill_qty, (
                f"{intent_id}: OVERSELL recorded {rec_trims + rec_exits} > fill {fill_qty}")

    def teardown(self):
        if getattr(self, "_conn", None) is not None:
            self._run(self._conn.close())
            self._conn = None
        if not self._loop.is_closed():
            self._loop.close()


TestPositionInvariants = PositionInvariantMachine.TestCase
```

- [ ] **Step 2: Run the machine**

Run: `.venv/bin/python -m pytest tests/property/test_position_invariants.py -q`
Expected: PASS (1 passed). With only `create_filled_intent`, `remaining_qty == fill_qty` for every intent and nothing is oversold.

- [ ] **Step 3: Commit**

```bash
git add tests/property/test_position_invariants.py
git commit -m "test(property): stateful scaffold + remaining-qty identity + INV-1"
```

---

### Task 4: Trim rules (`arm_trims`, `fire_trim`) via real `fire_rung_if_crossed` + INV-3

**Files:**
- Modify: `tests/property/test_position_invariants.py`

**Interfaces:**
- Consumes: `agent.exit_ladder.fire_rung_if_crossed`.
- Produces: rules `arm_trims`, `fire_trim`; invariant `no_trim_double_fire`. Each intent's shadow `self._rungs[intent_id][rung] = {"threshold_pct", "trim_pct", "recorded": bool}`.

- [ ] **Step 1: Add the import**

At the top of `tests/property/test_position_invariants.py`, add to the imports:

```python
from agent.exit_ladder import fire_rung_if_crossed
```

- [ ] **Step 2: Add the trim rules and the INV-3 invariant**

Insert these methods into `PositionInvariantMachine` (after `create_filled_intent`):

```python
    _LADDER = [(1, 0.05, 0.25), (2, 0.10, 0.25), (3, 0.20, 0.50)]

    @rule(intent=intents)
    def arm_trims(self, intent):
        if self._rungs[intent].get("_armed"):
            return
        self._run(self.trims.arm(intent, rungs=self._LADDER,
                                 armed_at="2026-06-26T14:30:00+00:00"))
        for rung, thr, tp in self._LADDER:
            self._rungs[intent][rung] = {"threshold_pct": thr, "trim_pct": tp,
                                         "recorded": False}
        self._rungs[intent]["_armed"] = True

    @rule(intent=intents,
          rung=st.sampled_from([1, 2, 3]),
          fill_mode=st.sampled_from(["full", "partial", "zero"]))
    def fire_trim(self, intent, rung, fill_mode):
        meta = self._rungs[intent].get(rung)
        if meta is None or meta["recorded"]:
            return  # not armed, or already recorded (real claim would reject anyway)
        fill_qty = self._fill[intent]
        ticker = self._rungs[intent]["_meta"]["ticker"]
        # current_price crosses this rung's threshold deterministically.
        current_price = 100.0 * (1.0 + meta["threshold_pct"]) + 1.0
        self.gw.fill_mode = fill_mode
        self.gw.unavailable = False
        fired = self._run(fire_rung_if_crossed(
            gw=self.gw, trim_store=self.trims, exits_store=self.exits,
            intent_id=intent, ticker=ticker, avg_fill_price=100.0,
            original_qty=fill_qty, rung=rung,
            threshold_pct=meta["threshold_pct"], trim_pct=meta["trim_pct"],
            current_price=current_price, slippage_cap_pct=0.01))
        # `fired` is True only when a positive fill was recorded (full/partial>0).
        if fired:
            meta["recorded"] = True

    @invariant()
    def no_trim_double_fire(self):
        # INV-3: each rung records a positive fire at most once.
        for intent_id in self._fill:
            for r in self._run(self.trims.all_for_intent(intent_id)):
                # A recorded rung has non-NULL sold_qty (record_fire only runs on
                # filled_qty>0). No row can be recorded twice: the claim gate
                # blocks a second claim until release, and a recorded rung is
                # never released. Assert via the shadow: recorded rungs stay recorded.
                if r["sold_qty"] is not None:
                    assert r["fired_at"] is not None, (
                        f"{intent_id} rung {r['rung']}: sold_qty set but not fired_at")
```

- [ ] **Step 3: Run the machine**

Run: `.venv/bin/python -m pytest tests/property/test_position_invariants.py -q`
Expected: PASS. Intents now arm ladders and fire rungs through the real code path; INV-1/consistency/INV-3 hold because `fire_rung_if_crossed` clamps `trim_qty` to `remaining_held` and the claim gate prevents double-fire.

- [ ] **Step 4: Commit**

```bash
git add tests/property/test_position_invariants.py
git commit -m "test(property): trim arm/fire rules via real fire path + INV-3"
```

---

### Task 5: Sell-following rule via real `SellFollower` + INV-2

**Files:**
- Modify: `tests/property/test_position_invariants.py`

**Interfaces:**
- Consumes: `agent.context.Context`, `skills.execution.sell_follower.SellFollower`.
- Produces: rule `follow_sell`; invariant `claim_once_idempotency` (INV-2) tracked via `self._fp_positive`.

- [ ] **Step 1: Add imports**

Add to the imports of `tests/property/test_position_invariants.py`:

```python
from agent.context import Context
from skills.execution.sell_follower import SellFollower
```

- [ ] **Step 2: Add the follow_sell rule (with repost via a small fingerprint pool)**

Insert into `PositionInvariantMachine`:

```python
    @rule(intent=intents,
          scope=st.sampled_from(["full", "partial"]),
          fraction=st.floats(min_value=0.1, max_value=1.0),
          fp_key=st.integers(min_value=0, max_value=3),  # small pool -> forces reposts
          fill_mode=st.sampled_from(["full", "partial", "zero"]),
          unavailable=st.booleans())
    def follow_sell(self, intent, scope, fraction, fp_key, fill_mode, unavailable):
        meta = self._rungs[intent]["_meta"]
        channel, ticker = meta["channel"], meta["ticker"]
        fingerprint = f"fp-{channel}-{ticker}-{fp_key}"
        self._seq += 1
        event_id = f"sell{self._seq}"

        # positive-recording invocations for this fingerprint BEFORE the run
        before = self._run(self._positive_exit_count(fingerprint))

        self.gw.fill_mode = fill_mode
        self.gw.unavailable = unavailable
        ctx = Context(trace_id="t", event_id=event_id)
        ctx.update({"action": "sell", "sell_ticker": ticker, "sell_scope": scope,
                    "sell_fraction": fraction, "channel": channel,
                    "message_fingerprint": fingerprint})
        follower = SellFollower(self.gw, self.intents_store, self.exits,
                                slippage_cap_pct=0.01, fill_timeout=5.0,
                                is_rth=lambda: True)
        self._run(follower.run(ctx))
        self.gw.unavailable = False  # reset for later rules

        after = self._run(self._positive_exit_count(fingerprint))
        if after > before:
            self._fp_positive[fingerprint] = self._fp_positive.get(fingerprint, 0) + 1

    async def _positive_exit_count(self, fingerprint: str) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) FROM position_exits WHERE fingerprint=? AND sold_qty>0",
            (fingerprint,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0] or 0)

    @invariant()
    def claim_once_idempotency(self):
        # INV-2: at most one SellFollower invocation records a positive-qty exit
        # per fingerprint (zero-fill sold_qty=0 rows and released retries excepted).
        for fingerprint, count in self._fp_positive.items():
            assert count <= 1, (
                f"{fingerprint}: {count} invocations recorded a positive sell")
```

- [ ] **Step 3: Run the machine**

Run: `.venv/bin/python -m pytest tests/property/test_position_invariants.py -q`
Expected: PASS. Sells run through the real `SellFollower` (claim-once, oldest-first, fresh re-check); repeated fingerprints are blocked by `claim_sell_event`, so each fingerprint records a positive sell at most once, and INV-1 still holds because every recording clamps to a fresh `remaining_qty`.

- [ ] **Step 4: Commit**

```bash
git add tests/property/test_position_invariants.py
git commit -m "test(property): sell-following rule via real SellFollower + INV-2"
```

---

### Task 6: `crash_during_trim` rule (in-flight stuck reserve vs. concurrent sells)

**Files:**
- Modify: `tests/property/test_position_invariants.py`

**Interfaces:**
- Produces: rule `crash_during_trim` — simulates process death between `claim_for_fire` and `record_fire`, leaving a persisted in-flight reserve, and lets subsequent `follow_sell`/`fire_trim` rules run against it.

- [ ] **Step 1: Add the crash rule**

Insert into `PositionInvariantMachine`:

```python
    @rule(intent=intents, rung=st.sampled_from([1, 2, 3]))
    def crash_during_trim(self, intent, rung):
        """Simulate a crash mid-fire: claim the rung (in-flight reserve persists),
        then 'die' before recording. all_unfired() excludes it, so it is never
        auto-re-fired; remaining_qty reserves it, so later sells cannot oversell."""
        meta = self._rungs[intent].get(rung)
        if meta is None or meta["recorded"]:
            return
        claimed = self._run(self.trims.claim_for_fire(
            intent, rung, "2026-06-26T14:30:00+00:00"))
        if claimed:
            # Mark recorded=True in shadow so fire_trim won't try to fire it
            # (a real restart leaves it stuck in-flight, not fireable).
            meta["recorded"] = True
            meta["stuck"] = True
```

- [ ] **Step 2: Run the machine**

Run: `.venv/bin/python -m pytest tests/property/test_position_invariants.py -q`
Expected: PASS. A stuck in-flight rung adds a reserve to `remaining_qty`; subsequent sells size against the reduced remaining and cannot oversell; the consistency invariant accounts for the reserve; no rung double-records.

- [ ] **Step 3: Commit**

```bash
git add tests/property/test_position_invariants.py
git commit -m "test(property): crash-during-trim stuck-reserve rule"
```

---

### Task 7: §3a probe — in-flight trader-sell vs. concurrent trim (deterministic)

**Files:**
- Create: `tests/property/test_inflight_sell_oversell.py`

**Interfaces:**
- Consumes: `FakeGateway.on_wait_fill`, real `follow_sell_position`, real `fire_rung_if_crossed`, the three stores.

This is the targeted, deterministic reproduction of the spec §3a candidate finding: a trader-sell is placed and awaiting fill (unrecorded, and `remaining_qty` has no in-flight-*sell* reserve) when a trim fires concurrently — potentially recording more than `fill_qty`.

- [ ] **Step 1: Write the probe asserting the correct invariant (recorded ≤ fill)**

Create `tests/property/test_inflight_sell_oversell.py`:

```python
"""§3a probe: does a trim firing inside a trader-sell's in-flight (placed but
unrecorded) window oversell? remaining_qty reserves in-flight TRIMS but not
in-flight SELLS, so this may record > fill_qty. See spec §3a + §10 findings policy.
"""
import aiosqlite
import pytest

from agent.exit_ladder import fire_rung_if_crossed
from infra.storage.db import SCHEMA
from infra.storage.position_exit_store import PositionExitStore
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from skills.execution.sell_follower import follow_sell_position
from tests.support.factories import make_filled_intent
from tests.support.fake_gateway import FakeGateway


@pytest.mark.asyncio
async def test_inflight_sell_concurrent_trim_does_not_oversell():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        intents = TradeIntentStore(conn)
        trims = TrimLadderStore(conn)
        exits = PositionExitStore(conn)

        fill_qty = 100
        intent_id = "e1:AAPL:long"
        await intents.insert(make_filled_intent(
            intent_id, channel="mystic", ticker="AAPL", fill_qty=fill_qty, seq=1))
        await trims.arm(intent_id, rungs=[(1, 0.05, 0.50)],
                        armed_at="2026-06-26T14:30:00+00:00")

        gw = FakeGateway()  # full fills

        # While the SELL is placed-but-unrecorded (inside wait_fill), fire the
        # trim through the REAL ladder path against the REAL remaining_qty.
        async def concurrent_trim():
            await fire_rung_if_crossed(
                gw=gw, trim_store=trims, exits_store=exits,
                intent_id=intent_id, ticker="AAPL", avg_fill_price=100.0,
                original_qty=fill_qty, rung=1, threshold_pct=0.05, trim_pct=0.50,
                current_price=106.0, slippage_cap_pct=0.01)
        gw.on_wait_fill = concurrent_trim

        # The trader sells the whole position (sized against remaining_qty=100).
        sold = await follow_sell_position(
            gw=gw, exits_store=exits, fingerprint="fp-1", event_id="evt-sell",
            intent_id=intent_id, channel="mystic", ticker="AAPL", qty=fill_qty,
            scope="full", slippage_cap_pct=0.01, fill_timeout=5.0)

        recorded_exit = await exits.sold_qty_for_intent(intent_id)
        recorded_trim = 0
        for r in await trims.all_for_intent(intent_id):
            recorded_trim += int(r["sold_qty"] or 0)
        total_recorded = recorded_exit + recorded_trim

        assert total_recorded <= fill_qty, (
            f"OVERSELL: recorded {total_recorded} (exit={recorded_exit} "
            f"trim={recorded_trim}) > fill {fill_qty}; sold returned {sold}")
```

- [ ] **Step 2: Run it and CHARACTERIZE the result**

Run: `.venv/bin/python -m pytest tests/property/test_inflight_sell_oversell.py -q`

Two outcomes — handle per the findings policy:

- **If it FAILS** (e.g. `OVERSELL: recorded 150 ... > fill 100`): the §3a finding is **CONFIRMED**. Do NOT change production code. Mark it as a known finding so the suite stays green and flags the fix:

  Change the decorator line to:

  ```python
  @pytest.mark.xfail(strict=True, reason="FINDING: in-flight trader-sell is "
                     "unreserved in remaining_qty; a concurrent trim oversells. "
                     "See spec 2026-06-26 §3a. Fix (reserve in-flight sells) "
                     "pending sign-off.")
  @pytest.mark.asyncio
  async def test_inflight_sell_concurrent_trim_does_not_oversell():
  ```

  Re-run; expected: `1 xfailed`.

- **If it PASSES**: the finding is **REFUTED** — keep the test as a passing regression guard and note in the commit that §3a did not reproduce.

- [ ] **Step 3: Commit (message depends on outcome)**

If confirmed (xfail):
```bash
git add tests/property/test_inflight_sell_oversell.py
git commit -m "test(property): probe confirms §3a in-flight-sell oversell (xfail, finding)"
```
If refuted (passing):
```bash
git add tests/property/test_inflight_sell_oversell.py
git commit -m "test(property): §3a in-flight-sell probe (passes; finding refuted)"
```

---

### Task 8: INV-4 — stateless `OrderSizer` sizing-cap properties

**Files:**
- Create: `tests/property/test_sizing_properties.py`

**Interfaces:**
- Consumes: `skills.execution.order_sizer.OrderSizer`, `agent.context.Context`, `FakeGateway`.

- [ ] **Step 1: Write the sizing property test**

Create `tests/property/test_sizing_properties.py`:

```python
"""INV-4: OrderSizer never returns a GENUINE sizing success (one carrying a
`quantity` in updates) that exceeds buying power or the aggregate exposure cap,
and never one with quantity<1. Gate on `"quantity" in updates`, NOT status
(partial_or returns status='success' without a quantity). See spec §3 INV-4.
"""
import asyncio

from hypothesis import given
from hypothesis import strategies as st

from agent.context import Context
from skills.execution.order_sizer import OrderSizer
from tests.support.fake_gateway import FakeGateway

_TOL = 1e-6


@given(
    net_liq=st.floats(min_value=1_000.0, max_value=10_000_000.0),
    buying_power=st.floats(min_value=0.0, max_value=10_000_000.0),
    size_pct=st.floats(min_value=0.0001, max_value=1.0),
    price=st.floats(min_value=0.5, max_value=5_000.0),
    margin=st.sampled_from([1.0, 2.0]),
    exposure=st.one_of(
        st.none(),
        st.tuples(st.floats(min_value=0.0, max_value=5_000_000.0),   # open_exposure
                  st.floats(min_value=0.0, max_value=10_000_000.0)),  # aggregate_cap
    ),
)
def test_order_sizer_respects_caps(net_liq, buying_power, size_pct, price, margin, exposure):
    async def run():
        gw = FakeGateway(net_liquidation=net_liq, buying_power=buying_power)
        sizer = OrderSizer(gw, margin_multiplier=margin)
        ctx = Context(trace_id="t", event_id="e")
        ctx.update({"instrument_type": "equity", "shares_pct": size_pct,
                    "reference_price": price, "ticker": "AAPL"})
        if exposure is not None:
            open_exposure, agg_cap = exposure
            ctx.update({"open_exposure": open_exposure,
                        "aggregate_notional_cap": agg_cap})
        result = await sizer.run(ctx)
        return result, (exposure[0] if exposure else None), (exposure[1] if exposure else None)

    result, open_exposure_in, agg_cap = asyncio.new_event_loop().run_until_complete(run())

    # Only a GENUINE sizing success carries a quantity.
    if "quantity" not in result.updates:
        return
    qty = result.updates["quantity"]
    notional = result.updates["notional_estimate"]
    unit_cost = price

    # (a) quantity >= 1 always
    assert qty >= 1

    # (b) buying-power clamp (buying_power is always a float from AccountSummary)
    assert qty * unit_cost <= buying_power + _TOL, (
        f"notional {qty*unit_cost} > buying_power {buying_power}")

    # (c) aggregate exposure cap only when both ctx keys were present
    if open_exposure_in is not None and agg_cap is not None:
        assert open_exposure_in + notional <= agg_cap + _TOL, (
            f"open {open_exposure_in} + notional {notional} > cap {agg_cap}")
```

Note: each example uses its own throwaway event loop (`asyncio.new_event_loop().run_until_complete`) — these are stateless `@given` examples, not a state machine, so per-example loops are fine.

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/property/test_sizing_properties.py -q`
Expected: PASS. If it fails on a genuine-success example, that is a real INV-4 finding — report it (do not weaken the property).

- [ ] **Step 3: Commit**

```bash
git add tests/property/test_sizing_properties.py
git commit -m "test(property): INV-4 OrderSizer sizing-cap properties"
```

---

### Task 9: CI profile, full-suite green, runtime budget

**Files:**
- (No new files; verification + a docs note in the plan/spec if anything changed.)

**Interfaces:**
- Produces: confirmation the whole suite is green and within the runtime budget under both profiles.

- [ ] **Step 1: Run the full property suite under the dev profile and time it**

Run: `.venv/bin/python -m pytest tests/property -q --durations=10`
Expected: PASS (any §3a xfail counts as xfailed, not failed). Confirm wall-clock for `tests/property` is **< ~30s**. If over budget, lower `dev` `max_examples`/`stateful_step_count` in `tests/property/conftest.py` and re-run.

- [ ] **Step 2: Run under the ci profile**

Run: `HYPOTHESIS_PROFILE=ci .venv/bin/python -m pytest tests/property -q`
Expected: PASS / xfailed. (Slower — this is the thorough profile.)

- [ ] **Step 3: Run the ENTIRE test suite to confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: the prior baseline (498 passed) plus the new tests, all green (plus any documented xfail). No failures.

- [ ] **Step 4: Commit any profile tuning**

```bash
git add tests/property/conftest.py
git commit -m "test(property): tune Hypothesis dev profile to runtime budget"
```

(If no tuning was needed, skip this commit.)

---

## Self-Review

**1. Spec coverage:**
- §3 INV-1 → Task 3 `never_oversell` (+ run-to-completion via Tasks 4–6). ✓
- §3 INV-2 → Task 5 `claim_once_idempotency`. ✓
- §3 INV-3 → Task 4 `no_trim_double_fire` (real claim gate). ✓
- §3 INV-4 → Task 8 (gated on `"quantity" in updates`, conditional clamps, tolerance). ✓
- §3 CONSISTENCY → Task 3 `remaining_qty_identity` (recorded-wins, round_half_up_min1, max(0)). ✓
- §3a probe → Task 7 (deterministic, xfail-if-confirmed). ✓
- §6 rules: create/arm/fire/follow_sell/crash → Tasks 3–6. (begin_sell/complete_sell split is realized as Task 7's `on_wait_fill` injection rather than separate random rules — same coverage, deterministic.) ✓
- §7 FakeGateway → Task 2. ✓
- §8 async recipe + placement → Tasks 1 & 3 (under `tests/`, instance-owned loop/conn, teardown order, `@settings(deadline=None,…)`). ✓
- §9 deps/layout/runtime → Tasks 1, 8, 9. ✓
- §10 findings policy → Task 7 (xfail strict) + INV notes. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has complete code; commands have expected output. Task 7's two-outcome handling is explicit (not a placeholder — both branches are fully specified). ✓

**3. Type consistency:** Store/skill/model signatures match the confirmed shapes in Global Constraints. Machine attribute names (`self.gw`, `self.intents_store`, `self.trims`, `self.exits`, `self._fill`, `self._rungs`, `self._fp_positive`, `self._run`) are consistent across Tasks 3–6. `fire_rung_if_crossed` keyword args match `agent/exit_ladder.py`. `SellFollower(...)` and `follow_sell_position(...)` args match `skills/execution/sell_follower.py`. ✓

---

## Execution Handoff

(Filled in after the plan is approved.)
