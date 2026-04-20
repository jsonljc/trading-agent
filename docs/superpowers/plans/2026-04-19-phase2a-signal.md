# Phase 2a-Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the brittle notification-polling + screenshot capture path with an AX-observer bridge, add full-lifecycle intent classification, persist a first-class `ParsedTradeSignal` record, and gate execution on Telegram inline-keyboard approval.

**Architecture:** The Swift bridge is rewritten to use `AXObserver` (event mode) with a 5-second reconciliation sweep (fallback). The Python pipeline gains three new layers: domain finalization (`ParsedSignalWriter`, `SignalDispositionResolver`), rollout control (`SignalApprovalGate` via Telegram inline keyboard), and a durable `parsed_signals` DB table. `DesktopReader` and `NotificationPoller` are deleted.

**Tech Stack:** Python 3.11+, aiosqlite, httpx, anthropic SDK, pydantic v2, pytest-asyncio; Swift 5.9 with AppKit + ApplicationServices frameworks.

**Out of scope (Plan B):** IBKR integration, ChainLookup, ContractSelector, OrderSizer, OrderSubmitter, PositionRegistry.

---

## File Structure

### New files
| File | Responsibility |
|---|---|
| `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift` | Dual-mode bridge: AXObserver callbacks + 5s reconciliation sweep |
| `agent/models.py` | `ParsedTradeSignal` dataclass |
| `infra/storage/parsed_signal_store.py` | Insert + update approval for `parsed_signals` table |
| `skills/signal/trade_signal_extractor.py` | Replaces `trade_intent_detector.py`; full lifecycle intents |
| `skills/domain/__init__.py` | Package marker |
| `skills/domain/parsed_signal_writer.py` | Materialises `ParsedTradeSignal` from context |
| `skills/domain/signal_disposition_resolver.py` | Sets `disposition` field; skips non-actionable (redundant safety) |
| `skills/rollout/__init__.py` | Package marker |
| `skills/rollout/signal_approval_gate.py` | Telegram inline keyboard approval with timeout |
| `tests/unit/test_trade_signal_extractor.py` | Unit tests for new intents |
| `tests/unit/test_parsed_signal_writer.py` | Unit tests |
| `tests/unit/test_signal_disposition_resolver.py` | Unit tests |
| `tests/unit/test_signal_approval_gate.py` | Unit tests |
| `tests/integration/test_parsed_signal_store.py` | Integration tests against in-memory DB |
| `tests/e2e/test_phase2a_signal_pipeline.py` | End-to-end pipeline test |

### Modified files
| File | Change |
|---|---|
| `infra/storage/db.py` | Add `parsed_signals` table to `SCHEMA` |
| `agent/policy.py` | Add `ApprovalPolicy` model + field |
| `config/policy.yaml` | Add `approval_policy` section |
| `infra/telegram/client.py` | Add `send_message_with_keyboard`, `answer_callback_query`, `wait_for_callback` |
| `agent/registry.py` | Add `build_phase2a_signal_chain` |
| `main.py` | Use `build_phase2a_signal_chain` |
| `bridge/Sources/NotificationBridge/main.swift` | Use `AXDiscordWatcher` |

### Deleted files
| File | Reason |
|---|---|
| `bridge/Sources/NotificationBridge/NotificationPoller.swift` | Replaced by `AXDiscordWatcher` |
| `skills/signal/desktop_reader.py` | No longer needed |
| `tests/unit/test_desktop_reader.py` | Corresponding test |

---

## Task 0: AGENT_CONTRACT.md — behavioral contract file

**Do this first, before any other task.**

**Files:**
- Create: `AGENT_CONTRACT.md` (repo root)
- Modify: `skills/signal/trade_signal_extractor.py` (system prompt header)
- Modify: `skills/signal/ticker_resolver.py` (system prompt header)
- Modify: `skills/signal/conviction_classifier.py` (system prompt header)

- [ ] **Step 1: Create `AGENT_CONTRACT.md` at repo root**

Create `/Users/jasonli/dev/trading-agent/AGENT_CONTRACT.md`:

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
- Only select contracts that pass liquidity guards (e.g., min_bid, max_spread_pct,
  and other configured liquidity thresholds)
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

- [ ] **Step 2: Add contract citation header to `skills/signal/trade_signal_extractor.py`**

Open `skills/signal/trade_signal_extractor.py`. Change the first line of `_SYSTEM_PROMPT` from:

```python
_SYSTEM_PROMPT = """Classify a Discord trading message into one of six signal types.
```

to:

```python
_SYSTEM_PROMPT = """Rules: see AGENT_CONTRACT.md in repo root.

Classify a Discord trading message into one of six signal types.
```

- [ ] **Step 3: Add contract citation header to `skills/signal/ticker_resolver.py`**

Open `skills/signal/ticker_resolver.py`. Change the first line of `_SYSTEM_PROMPT` from:

```python
_SYSTEM_PROMPT = """Extract the stock ticker from a trading message.
```

to:

```python
_SYSTEM_PROMPT = """Rules: see AGENT_CONTRACT.md in repo root.

Extract the stock ticker from a trading message.
```

- [ ] **Step 4: Add contract citation header to `skills/signal/conviction_classifier.py`**

Open `skills/signal/conviction_classifier.py`. Change the first line of `_SYSTEM_PROMPT` from:

```python
_SYSTEM_PROMPT = """Classify trading message conviction as 'high' or 'low'.
```

to:

```python
_SYSTEM_PROMPT = """Rules: see AGENT_CONTRACT.md in repo root.

Classify trading message conviction as 'high' or 'low'.
```

- [ ] **Step 5: Verify file exists and import chain still works**

```bash
cd ~/dev/trading-agent
test -f AGENT_CONTRACT.md && echo "contract exists"
python3 -c "from skills.signal.trade_signal_extractor import TradeSignalExtractor; print('ok')"
python3 -c "from skills.signal.ticker_resolver import TickerResolver; print('ok')"
python3 -c "from skills.signal.conviction_classifier import ConvictionClassifier; print('ok')"
```

Expected: all four lines print without error.

- [ ] **Step 6: Commit**

```bash
git add AGENT_CONTRACT.md \
        skills/signal/trade_signal_extractor.py \
        skills/signal/ticker_resolver.py \
        skills/signal/conviction_classifier.py
git commit -m "feat(contract): add AGENT_CONTRACT.md behavioral contract + cite in LLM skill prompts"
```

---

## Task 1: DB schema — add parsed_signals table

**Files:**
- Modify: `infra/storage/db.py`
- Test: `tests/integration/test_parsed_signal_store.py` (written in Task 3)

- [ ] **Step 1: Add `parsed_signals` table to SCHEMA in `infra/storage/db.py`**

Open `infra/storage/db.py`. Append the following table definition inside the `SCHEMA` string, after the last `CREATE TABLE` block:

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
CREATE TABLE IF NOT EXISTS parsed_signals (
    id TEXT PRIMARY KEY,
    trace_id TEXT,
    source TEXT,
    source_message_fingerprint TEXT,
    author TEXT,
    channel TEXT,
    raw_text TEXT,
    normalized_text TEXT,
    signal_type TEXT,
    ticker TEXT,
    asset_type_hint TEXT,
    side TEXT,
    conviction_bucket TEXT,
    parse_confidence TEXT,
    ambiguity_flags TEXT,
    target_allocation_pct REAL,
    created_at TEXT,
    approved_at TEXT,
    approval_status TEXT DEFAULT 'pending',
    telegram_message_id INTEGER
);
"""
```

- [ ] **Step 2: Verify schema applies cleanly**

```bash
cd ~/dev/trading-agent
python3 -c "
import asyncio, aiosqlite
from infra.storage.db import SCHEMA
async def check():
    async with aiosqlite.connect(':memory:') as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
        async with conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\") as cur:
            rows = await cur.fetchall()
            print([r[0] for r in rows])
asyncio.run(check())
"
```

Expected output contains: `['signal_events', 'idempotency_keys', 'work_traces', 'skill_outputs', 'parsed_signals']`

- [ ] **Step 3: Commit**

```bash
git add infra/storage/db.py
git commit -m "feat(db): add parsed_signals table to schema"
```

---

## Task 2: ParsedTradeSignal model

**Files:**
- Create: `agent/models.py`

- [ ] **Step 1: Write the model**

Create `agent/models.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedTradeSignal:
    id: str
    trace_id: str
    source: str                    # "ax_event" | "reconciliation" | "injected"
    source_message_fingerprint: str
    author: str
    channel: str
    raw_text: str
    normalized_text: str
    signal_type: str               # LONG_SIGNAL | ADD_SIGNAL | CLOSE_SIGNAL | PARTIAL_CLOSE | WATCHLIST_ONLY | NO_ACTION
    ticker: Optional[str]
    asset_type_hint: str           # "equity" | "option"
    side: Optional[str]            # "long" | "close" | None
    conviction_bucket: str         # "high" | "low"
    parse_confidence: str          # "high" | "medium" | "low"
    ambiguity_flags: list[str]     = field(default_factory=list)
    target_allocation_pct: float   = 0.0
    created_at: str                = ""
    approved_at: Optional[str]     = None
    approval_status: str           = "pending"
    telegram_message_id: Optional[int] = None
```

- [ ] **Step 2: Verify import**

```bash
python3 -c "from agent.models import ParsedTradeSignal; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add agent/models.py
git commit -m "feat(models): add ParsedTradeSignal dataclass"
```

---

## Task 3: ParsedSignalStore

**Files:**
- Create: `infra/storage/parsed_signal_store.py`
- Test: `tests/integration/test_parsed_signal_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_parsed_signal_store.py`:

```python
import pytest
from agent.models import ParsedTradeSignal
from infra.storage.parsed_signal_store import ParsedSignalStore


def make_signal(**overrides) -> ParsedTradeSignal:
    base = dict(
        id="sig-1",
        trace_id="t-1",
        source="injected",
        source_message_fingerprint="fp1",
        author="Mystic",
        channel="mystic",
        raw_text="Long $AVEX",
        normalized_text="Long $AVEX",
        signal_type="LONG_SIGNAL",
        ticker="AVEX",
        asset_type_hint="option",
        side="long",
        conviction_bucket="high",
        parse_confidence="high",
        ambiguity_flags=[],
        target_allocation_pct=0.10,
        created_at="2026-04-19T10:00:00Z",
        approval_status="pending",
    )
    base.update(overrides)
    return ParsedTradeSignal(**base)


async def test_insert_and_get(db):
    store = ParsedSignalStore(db)
    sig = make_signal()
    await store.insert(sig)
    row = await store.get_by_id("sig-1")
    assert row is not None
    assert row["signal_type"] == "LONG_SIGNAL"
    assert row["ticker"] == "AVEX"
    assert row["approval_status"] == "pending"


async def test_insert_idempotent(db):
    store = ParsedSignalStore(db)
    sig = make_signal()
    await store.insert(sig)
    await store.insert(sig)  # second insert should not raise
    row = await store.get_by_id("sig-1")
    assert row is not None


async def test_update_approval(db):
    store = ParsedSignalStore(db)
    await store.insert(make_signal())
    await store.update_approval(
        signal_id="sig-1",
        status="approved",
        approved_at="2026-04-19T10:00:30Z",
        telegram_message_id=42,
    )
    row = await store.get_by_id("sig-1")
    assert row["approval_status"] == "approved"
    assert row["approved_at"] == "2026-04-19T10:00:30Z"
    assert row["telegram_message_id"] == 42


async def test_ambiguity_flags_roundtrip(db):
    store = ParsedSignalStore(db)
    sig = make_signal(ambiguity_flags=["ticker_ambiguous", "intent_uncertain"])
    await store.insert(sig)
    row = await store.get_by_id("sig-1")
    import json
    assert json.loads(row["ambiguity_flags"]) == ["ticker_ambiguous", "intent_uncertain"]
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd ~/dev/trading-agent
pytest tests/integration/test_parsed_signal_store.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `parsed_signal_store` doesn't exist yet.

- [ ] **Step 3: Implement ParsedSignalStore**

Create `infra/storage/parsed_signal_store.py`:

```python
from __future__ import annotations
import json
import aiosqlite
from agent.models import ParsedTradeSignal


class ParsedSignalStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, signal: ParsedTradeSignal) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO parsed_signals (
                id, trace_id, source, source_message_fingerprint,
                author, channel, raw_text, normalized_text,
                signal_type, ticker, asset_type_hint, side,
                conviction_bucket, parse_confidence, ambiguity_flags,
                target_allocation_pct, created_at, approved_at,
                approval_status, telegram_message_id
            ) VALUES (
                :id, :trace_id, :source, :source_message_fingerprint,
                :author, :channel, :raw_text, :normalized_text,
                :signal_type, :ticker, :asset_type_hint, :side,
                :conviction_bucket, :parse_confidence, :ambiguity_flags,
                :target_allocation_pct, :created_at, :approved_at,
                :approval_status, :telegram_message_id
            )""",
            {
                "id": signal.id,
                "trace_id": signal.trace_id,
                "source": signal.source,
                "source_message_fingerprint": signal.source_message_fingerprint,
                "author": signal.author,
                "channel": signal.channel,
                "raw_text": signal.raw_text,
                "normalized_text": signal.normalized_text,
                "signal_type": signal.signal_type,
                "ticker": signal.ticker,
                "asset_type_hint": signal.asset_type_hint,
                "side": signal.side,
                "conviction_bucket": signal.conviction_bucket,
                "parse_confidence": signal.parse_confidence,
                "ambiguity_flags": json.dumps(signal.ambiguity_flags),
                "target_allocation_pct": signal.target_allocation_pct,
                "created_at": signal.created_at,
                "approved_at": signal.approved_at,
                "approval_status": signal.approval_status,
                "telegram_message_id": signal.telegram_message_id,
            },
        )
        await self._conn.commit()

    async def update_approval(
        self,
        signal_id: str,
        status: str,
        approved_at: str | None,
        telegram_message_id: int | None,
    ) -> None:
        await self._conn.execute(
            """UPDATE parsed_signals
               SET approval_status=?, approved_at=?, telegram_message_id=?
               WHERE id=?""",
            (status, approved_at, telegram_message_id, signal_id),
        )
        await self._conn.commit()

    async def get_by_id(self, signal_id: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM parsed_signals WHERE id=?", (signal_id,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return dict(row)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/integration/test_parsed_signal_store.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/storage/parsed_signal_store.py tests/integration/test_parsed_signal_store.py
git commit -m "feat(storage): add ParsedSignalStore"
```

---

## Task 4: Policy — add ApprovalPolicy

**Files:**
- Modify: `agent/policy.py`
- Modify: `config/policy.yaml`
- Test: `tests/unit/test_policy.py` (extend existing)

- [ ] **Step 1: Write the failing test**

Open `tests/unit/test_policy.py`. Add:

```python
def test_approval_policy_defaults():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    policy = PolicyModel.model_validate(yaml.safe_load(config_path.read_text()))
    assert policy.approval_policy.approval_required is True
    assert policy.approval_policy.auto_approve_closes is True
    assert policy.approval_policy.approval_timeout_secs == 60
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_policy.py::test_approval_policy_defaults -v
```

Expected: `AttributeError: 'PolicyModel' object has no attribute 'approval_policy'`

- [ ] **Step 3: Add ApprovalPolicy and HarnessPolicy to `agent/policy.py`**

Add both classes before `PolicyModel`, then add both fields to `PolicyModel`:

```python
class ApprovalPolicy(BaseModel):
    approval_required: bool = True
    auto_approve_closes: bool = True
    approval_timeout_secs: int = 60


class HarnessPolicy(BaseModel):
    dry_run: bool = False
    dry_run_auto_approve: bool = False
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
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    harness: HarnessPolicy = HarnessPolicy()
```

- [ ] **Step 4: Add `approval_policy` and `harness` to `config/policy.yaml`**

Append to the end of `config/policy.yaml`:

```yaml
approval_policy:
  approval_required: true
  auto_approve_closes: true
  approval_timeout_secs: 60

harness:
  dry_run: false
  dry_run_auto_approve: false
```

- [ ] **Step 5: Run — expect pass**

```bash
pytest tests/unit/test_policy.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/policy.py config/policy.yaml tests/unit/test_policy.py
git commit -m "feat(policy): add ApprovalPolicy and HarnessPolicy (dry-run support)"
```

---

## Task 5: TradeSignalExtractor

**Files:**
- Create: `skills/signal/trade_signal_extractor.py`
- Test: `tests/unit/test_trade_signal_extractor.py`

The existing `TradeIntentDetector` handles `LONG_SIGNAL | ADD_SIGNAL | WATCHLIST_ONLY | NO_ACTION`. This replaces it with `CLOSE_SIGNAL | PARTIAL_CLOSE` added.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_trade_signal_extractor.py`:

```python
import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from agent.policy import PolicyModel
import yaml


def make_policy():
    return PolicyModel.model_validate(
        yaml.safe_load((Path(__file__).parents[2] / "config" / "policy.yaml").read_text())
    )


def make_ctx(text: str) -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"full_message_text": text, "channel": "mystic", "author": "Mystic"})
    return ctx


def claude_resp(signal_type: str, confidence: str = "high", reason: str = "test") -> MagicMock:
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps({
        "signal_type": signal_type, "confidence": confidence, "reason": reason
    }))]
    return m


async def test_long_signal(monkeypatch):
    from skills.signal.trade_signal_extractor import TradeSignalExtractor
    skill = TradeSignalExtractor(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=claude_resp("LONG_SIGNAL")))
    result = await skill.run(make_ctx("Long $AVEX initiating a position"))
    assert result.status == "success"
    assert result.updates["signal_type"] == "LONG_SIGNAL"
    assert result.updates["confidence"] == "high"


async def test_close_signal(monkeypatch):
    from skills.signal.trade_signal_extractor import TradeSignalExtractor
    skill = TradeSignalExtractor(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=claude_resp("CLOSE_SIGNAL")))
    result = await skill.run(make_ctx("Out of AVEX, took profits"))
    assert result.status == "success"
    assert result.updates["signal_type"] == "CLOSE_SIGNAL"


async def test_partial_close(monkeypatch):
    from skills.signal.trade_signal_extractor import TradeSignalExtractor
    skill = TradeSignalExtractor(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=claude_resp("PARTIAL_CLOSE")))
    result = await skill.run(make_ctx("Trimming half my AVEX here"))
    assert result.status == "success"
    assert result.updates["signal_type"] == "PARTIAL_CLOSE"


async def test_watchlist_skips(monkeypatch):
    from skills.signal.trade_signal_extractor import TradeSignalExtractor
    skill = TradeSignalExtractor(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=claude_resp("WATCHLIST_ONLY")))
    result = await skill.run(make_ctx("Watching AVEX closely"))
    assert result.status == "skip"
    assert "WATCHLIST_ONLY" in result.reason


async def test_no_action_skips(monkeypatch):
    from skills.signal.trade_signal_extractor import TradeSignalExtractor
    skill = TradeSignalExtractor(make_policy())
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=claude_resp("NO_ACTION")))
    result = await skill.run(make_ctx("Interesting market today"))
    assert result.status == "skip"


async def test_parse_error_fails(monkeypatch):
    from skills.signal.trade_signal_extractor import TradeSignalExtractor
    skill = TradeSignalExtractor(make_policy())
    m = MagicMock()
    m.content = [MagicMock(text="not json at all")]
    monkeypatch.setattr(skill._client.messages, "create", AsyncMock(return_value=m))
    result = await skill.run(make_ctx("some message"))
    assert result.status == "fail"
    assert "parse error" in result.reason
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_trade_signal_extractor.py -v
```

Expected: `ImportError` — module doesn't exist yet.

- [ ] **Step 3: Implement TradeSignalExtractor**

Create `skills/signal/trade_signal_extractor.py`:

```python
from __future__ import annotations
import json
import re
import logging
import anthropic
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Classify a Discord trading message into one of six signal types.

LONG_SIGNAL: author is initiating a new long position.
ADD_SIGNAL: author is adding to an existing long position.
CLOSE_SIGNAL: author is fully exiting / closing a position ("out", "sold", "took profits", "closed").
PARTIAL_CLOSE: author is trimming part of a position ("trimming half", "sold 1/3", "reducing").
WATCHLIST_ONLY: author is observing but not acting ("watching", "on radar", "monitoring").
NO_ACTION: commentary, news, analysis — not actionable.

Action words for LONG_SIGNAL: long, initiating, starting position, entered, bought.
Action words for ADD_SIGNAL: adding, adding to, scaling in, second tranche.
Action words for CLOSE_SIGNAL: out, sold, closed, took profits, exiting, flat.
Action words for PARTIAL_CLOSE: trimming, reducing, sold half, took partial, partial exit.

Respond with valid JSON only:
{"signal_type": "LONG_SIGNAL|ADD_SIGNAL|CLOSE_SIGNAL|PARTIAL_CLOSE|WATCHLIST_ONLY|NO_ACTION", "confidence": "high|medium|low", "reason": "one sentence"}"""

_VALID_TYPES = frozenset(
    {"LONG_SIGNAL", "ADD_SIGNAL", "CLOSE_SIGNAL", "PARTIAL_CLOSE", "WATCHLIST_ONLY", "NO_ACTION"}
)


def _safe_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


class TradeSignalExtractor(Skill):
    name = "trade_signal_extractor"

    def __init__(self, policy: PolicyModel) -> None:
        self._policy = policy
        self._client = anthropic.AsyncAnthropic()

    async def run(self, ctx: Context) -> SkillResult:
        text = ctx.get("full_message_text", "")
        channel = ctx.get("channel", "")
        author = ctx.get("author", "")

        response = await self._client.messages.create(
            model=self._policy.models.text,
            max_tokens=256,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"Channel: #{channel}\nAuthor: {author}\nMessage: {text}"}],
        )

        parsed = _safe_json(response.content[0].text)
        if not parsed or "signal_type" not in parsed:
            return SkillResult(
                status="fail",
                reason=f"trade_signal_extractor parse error: {response.content[0].text[:100]}",
            )

        signal_type = parsed["signal_type"]
        if signal_type not in _VALID_TYPES:
            return SkillResult(status="fail", reason=f"unknown signal_type: {signal_type}")

        confidence = parsed.get("confidence", "medium")
        reason = parsed.get("reason", "")

        if signal_type in ("NO_ACTION", "WATCHLIST_ONLY"):
            return SkillResult(status="skip", reason=f"{signal_type}: {reason}")

        return SkillResult(
            status="success",
            updates={"signal_type": signal_type, "confidence": confidence, "reason": reason},
        )
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/unit/test_trade_signal_extractor.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/signal/trade_signal_extractor.py tests/unit/test_trade_signal_extractor.py
git commit -m "feat(skills): add TradeSignalExtractor with full lifecycle intents"
```

---

## Task 6: ParsedSignalWriter skill

**Files:**
- Create: `skills/domain/__init__.py`
- Create: `skills/domain/parsed_signal_writer.py`
- Test: `tests/unit/test_parsed_signal_writer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_parsed_signal_writer.py`:

```python
import pytest
from unittest.mock import AsyncMock
from agent.context import Context
from agent.models import ParsedTradeSignal


def make_ctx(signal_type: str = "LONG_SIGNAL", ticker: str | None = "AVEX") -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "source": "injected",
        "trigger_preview": "Long $AVEX",
        "full_message_text": "Long $AVEX",
        "message_fingerprint": "fp123",
        "author": "Mystic",
        "channel": "mystic",
        "signal_type": signal_type,
        "ticker": ticker,
        "asset_type_hint": "option",
        "confidence": "high",
        "conviction_bucket": "high",
        "target_allocation_pct": 0.10,
        "ambiguity_flags": [],
    })
    return ctx


async def test_writes_signal_and_sets_context_id(db):
    from infra.storage.parsed_signal_store import ParsedSignalStore
    from skills.domain.parsed_signal_writer import ParsedSignalWriter
    store = ParsedSignalStore(db)
    skill = ParsedSignalWriter(store)
    ctx = make_ctx()
    result = await skill.run(ctx)
    assert result.status == "success"
    signal_id = result.updates["parsed_signal_id"]
    assert signal_id is not None
    row = await store.get_by_id(signal_id)
    assert row["signal_type"] == "LONG_SIGNAL"
    assert row["ticker"] == "AVEX"
    assert row["side"] == "long"


async def test_close_signal_sets_side_close(db):
    from infra.storage.parsed_signal_store import ParsedSignalStore
    from skills.domain.parsed_signal_writer import ParsedSignalWriter
    store = ParsedSignalStore(db)
    skill = ParsedSignalWriter(store)
    ctx = make_ctx(signal_type="CLOSE_SIGNAL")
    result = await skill.run(ctx)
    row = await store.get_by_id(result.updates["parsed_signal_id"])
    assert row["side"] == "close"


async def test_partial_close_sets_side_close(db):
    from infra.storage.parsed_signal_store import ParsedSignalStore
    from skills.domain.parsed_signal_writer import ParsedSignalWriter
    store = ParsedSignalStore(db)
    skill = ParsedSignalWriter(store)
    ctx = make_ctx(signal_type="PARTIAL_CLOSE")
    result = await skill.run(ctx)
    row = await store.get_by_id(result.updates["parsed_signal_id"])
    assert row["side"] == "close"
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_parsed_signal_writer.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Create package file and implement**

```bash
touch ~/dev/trading-agent/skills/domain/__init__.py
```

Create `skills/domain/parsed_signal_writer.py`:

```python
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.models import ParsedTradeSignal
from infra.storage.parsed_signal_store import ParsedSignalStore

_CLOSE_TYPES = frozenset({"CLOSE_SIGNAL", "PARTIAL_CLOSE"})
_LONG_TYPES = frozenset({"LONG_SIGNAL", "ADD_SIGNAL"})


class ParsedSignalWriter(Skill):
    name = "parsed_signal_writer"

    def __init__(self, store: ParsedSignalStore) -> None:
        self._store = store

    async def run(self, ctx: Context) -> SkillResult:
        signal_type = ctx.get("signal_type", "NO_ACTION")
        side: str | None
        if signal_type in _CLOSE_TYPES:
            side = "close"
        elif signal_type in _LONG_TYPES:
            side = "long"
        else:
            side = None

        signal = ParsedTradeSignal(
            id=str(uuid.uuid4()),
            trace_id=ctx.trace_id,
            source=ctx.get("source", "ax_event"),
            source_message_fingerprint=ctx.get("message_fingerprint", ""),
            author=ctx.get("author", ""),
            channel=ctx.get("channel", ""),
            raw_text=ctx.get("trigger_preview", ""),
            normalized_text=ctx.get("full_message_text", ""),
            signal_type=signal_type,
            ticker=ctx.get("ticker"),
            asset_type_hint=ctx.get("asset_type_hint", "equity"),
            side=side,
            conviction_bucket=ctx.get("conviction_bucket", "low"),
            parse_confidence=ctx.get("confidence", "medium"),
            ambiguity_flags=ctx.get("ambiguity_flags", []),
            target_allocation_pct=ctx.get("target_allocation_pct", 0.0),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._store.insert(signal)
        return SkillResult(status="success", updates={"parsed_signal_id": signal.id})
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_parsed_signal_writer.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/domain/__init__.py skills/domain/parsed_signal_writer.py tests/unit/test_parsed_signal_writer.py
git commit -m "feat(skills): add ParsedSignalWriter domain skill"
```

---

## Task 7: SignalDispositionResolver skill

**Files:**
- Create: `skills/domain/signal_disposition_resolver.py`
- Test: `tests/unit/test_signal_disposition_resolver.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_signal_disposition_resolver.py`:

```python
import pytest
from agent.context import Context
from skills.domain.signal_disposition_resolver import SignalDispositionResolver


def make_ctx(signal_type: str) -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"signal_type": signal_type})
    return ctx


async def test_long_signal_returns_open():
    skill = SignalDispositionResolver()
    result = await skill.run(make_ctx("LONG_SIGNAL"))
    assert result.status == "success"
    assert result.updates["disposition"] == "open"


async def test_add_signal_returns_add():
    skill = SignalDispositionResolver()
    result = await skill.run(make_ctx("ADD_SIGNAL"))
    assert result.status == "success"
    assert result.updates["disposition"] == "add"


async def test_close_signal_returns_close():
    skill = SignalDispositionResolver()
    result = await skill.run(make_ctx("CLOSE_SIGNAL"))
    assert result.status == "success"
    assert result.updates["disposition"] == "close"


async def test_partial_close_returns_partial_close():
    skill = SignalDispositionResolver()
    result = await skill.run(make_ctx("PARTIAL_CLOSE"))
    assert result.status == "success"
    assert result.updates["disposition"] == "partial_close"


async def test_watchlist_skips():
    skill = SignalDispositionResolver()
    result = await skill.run(make_ctx("WATCHLIST_ONLY"))
    assert result.status == "skip"


async def test_no_action_skips():
    skill = SignalDispositionResolver()
    result = await skill.run(make_ctx("NO_ACTION"))
    assert result.status == "skip"
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_signal_disposition_resolver.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement**

Create `skills/domain/signal_disposition_resolver.py`:

```python
from agent.context import Context, SkillResult
from agent.skill import Skill

_DISPOSITION_MAP = {
    "LONG_SIGNAL":    "open",
    "ADD_SIGNAL":     "add",
    "CLOSE_SIGNAL":   "close",
    "PARTIAL_CLOSE":  "partial_close",
}


class SignalDispositionResolver(Skill):
    name = "signal_disposition_resolver"

    async def run(self, ctx: Context) -> SkillResult:
        signal_type = ctx.get("signal_type", "NO_ACTION")
        disposition = _DISPOSITION_MAP.get(signal_type)
        if disposition is None:
            return SkillResult(status="skip", reason=f"{signal_type}: non-actionable")
        return SkillResult(status="success", updates={"disposition": disposition})
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_signal_disposition_resolver.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/domain/signal_disposition_resolver.py tests/unit/test_signal_disposition_resolver.py
git commit -m "feat(skills): add SignalDispositionResolver"
```

---

## Task 8: TelegramClient — keyboard + callback polling

**Files:**
- Modify: `infra/telegram/client.py`
- Test: `tests/integration/test_telegram_client.py` (extend existing)

- [ ] **Step 1: Write failing tests**

Open `tests/integration/test_telegram_client.py`. Add these tests (they will fail until the client is updated):

```python
async def test_send_message_with_keyboard_calls_correct_endpoint(respx_mock):
    import respx, httpx
    from infra.telegram.client import TelegramClient
    respx_mock.post("https://api.telegram.org/botTEST/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
    )
    client = TelegramClient(bot_token="TEST", chat_id="CHAT")
    msg_id = await client.send_message_with_keyboard(
        "<b>Signal</b>",
        [[{"text": "✅ Approve", "callback_data": "approved"},
          {"text": "❌ Reject", "callback_data": "rejected"}]]
    )
    assert msg_id == 99


async def test_wait_for_callback_returns_approved(respx_mock):
    import respx, httpx, json
    from infra.telegram.client import TelegramClient

    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cq1",
            "data": "approved",
            "message": {"message_id": 99},
        }
    }
    respx_mock.get("https://api.telegram.org/botTEST/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [update]})
    )
    respx_mock.post("https://api.telegram.org/botTEST/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = TelegramClient(bot_token="TEST", chat_id="CHAT")
    result = await client.wait_for_callback(message_id=99, timeout_secs=5)
    assert result == "approved"


async def test_wait_for_callback_times_out():
    import asyncio
    from infra.telegram.client import TelegramClient

    async def empty_updates(*a, **kw):
        await asyncio.sleep(0)
        return []

    client = TelegramClient(bot_token="TEST", chat_id="CHAT")
    # Monkeypatch _get_updates to return nothing immediately
    client._get_updates = empty_updates  # type: ignore[method-assign]
    result = await client.wait_for_callback(message_id=99, timeout_secs=1)
    assert result == "timeout"
```

Note: these tests require `respx` for HTTP mocking. Add it to `pyproject.toml` dev deps.

- [ ] **Step 2: Add respx to dev deps in `pyproject.toml`**

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.12",
    "respx>=0.20",
]
```

```bash
pip install respx
```

- [ ] **Step 3: Run — expect failure**

```bash
pytest tests/integration/test_telegram_client.py -v -k "keyboard or callback"
```

Expected: `AttributeError` — method doesn't exist yet.

- [ ] **Step 4: Extend TelegramClient**

Replace the full contents of `infra/telegram/client.py`:

```python
from __future__ import annotations
import asyncio
import httpx


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
            )
            if resp.is_error:
                raise httpx.HTTPStatusError(
                    f"Telegram error {resp.status_code}: {resp.text}",
                    request=resp.request,
                    response=resp,
                )

    async def send_message_with_keyboard(
        self, text: str, inline_keyboard: list[list[dict]]
    ) -> int:
        """Send message with inline keyboard buttons. Returns message_id."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": inline_keyboard},
                },
            )
            resp.raise_for_status()
            return resp.json()["result"]["message_id"]

    async def answer_callback_query(self, callback_query_id: str) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id},
            )

    async def _get_updates(self, offset: int = 0, timeout: int = 1) -> list[dict]:
        async with httpx.AsyncClient(timeout=timeout + 5) as client:
            resp = await client.get(
                f"{self._base}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": timeout,
                    "allowed_updates": '["callback_query"]',
                },
            )
            if resp.is_error:
                return []
            return resp.json().get("result", [])

    async def wait_for_callback(self, message_id: int, timeout_secs: int) -> str:
        """
        Long-poll for an inline keyboard callback for the given message_id.
        Returns 'approved', 'rejected', or 'timeout'.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_secs
        offset = 0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout"
            poll_secs = min(int(remaining), 25)
            updates = await self._get_updates(offset=offset, timeout=poll_secs)
            for update in updates:
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue
                if cq.get("message", {}).get("message_id") == message_id:
                    await self.answer_callback_query(cq["id"])
                    return cq.get("data", "rejected")
```

- [ ] **Step 5: Run all telegram tests — expect pass**

```bash
pytest tests/integration/test_telegram_client.py -v
```

Expected: all tests PASS (including pre-existing ones).

- [ ] **Step 6: Commit**

```bash
git add infra/telegram/client.py tests/integration/test_telegram_client.py pyproject.toml
git commit -m "feat(telegram): add keyboard message + callback polling"
```

---

## Task 9: SignalApprovalGate skill

**Files:**
- Create: `skills/rollout/__init__.py`
- Create: `skills/rollout/signal_approval_gate.py`
- Test: `tests/unit/test_signal_approval_gate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_signal_approval_gate.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from agent.models import ParsedTradeSignal
from agent.policy import PolicyModel, ApprovalPolicy
import yaml
from pathlib import Path


def make_policy(**harness_overrides) -> PolicyModel:
    raw = yaml.safe_load((Path(__file__).parents[2] / "config" / "policy.yaml").read_text())
    if harness_overrides:
        raw.setdefault("harness", {}).update(harness_overrides)
    return PolicyModel.model_validate(raw)


def make_ctx(signal_type: str = "LONG_SIGNAL", signal_id: str = "sig-1") -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "signal_type": signal_type,
        "parsed_signal_id": signal_id,
        "ticker": "AVEX",
        "author": "Mystic",
        "channel": "mystic",
        "full_message_text": "Long $AVEX",
        "conviction_bucket": "high",
        "target_allocation_pct": 0.10,
    })
    return ctx


class FakeStore:
    def __init__(self):
        self.calls = []
    async def update_approval(self, signal_id, status, approved_at, telegram_message_id):
        self.calls.append((signal_id, status, approved_at, telegram_message_id))


async def test_approved_returns_success():
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    telegram = MagicMock()
    telegram.send_message_with_keyboard = AsyncMock(return_value=42)
    telegram.wait_for_callback = AsyncMock(return_value="approved")
    store = FakeStore()
    skill = SignalApprovalGate(make_policy(), telegram, store)
    result = await skill.run(make_ctx())
    assert result.status == "success"
    assert result.updates["approval_status"] == "approved"
    assert store.calls[0][1] == "approved"


async def test_rejected_returns_skip():
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    telegram = MagicMock()
    telegram.send_message_with_keyboard = AsyncMock(return_value=42)
    telegram.wait_for_callback = AsyncMock(return_value="rejected")
    store = FakeStore()
    skill = SignalApprovalGate(make_policy(), telegram, store)
    result = await skill.run(make_ctx())
    assert result.status == "skip"
    assert "rejected" in result.reason


async def test_timeout_returns_skip():
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    telegram = MagicMock()
    telegram.send_message_with_keyboard = AsyncMock(return_value=42)
    telegram.wait_for_callback = AsyncMock(return_value="timeout")
    store = FakeStore()
    skill = SignalApprovalGate(make_policy(), telegram, store)
    result = await skill.run(make_ctx())
    assert result.status == "skip"
    assert "timeout" in result.reason


async def test_close_signal_auto_approves():
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    telegram = MagicMock()
    telegram.send_message_with_keyboard = AsyncMock()
    store = FakeStore()
    policy = make_policy()
    policy.approval_policy.auto_approve_closes = True
    skill = SignalApprovalGate(policy, telegram, store)
    result = await skill.run(make_ctx(signal_type="CLOSE_SIGNAL"))
    assert result.status == "success"
    assert result.updates["approval_status"] == "approved"
    telegram.send_message_with_keyboard.assert_not_called()


async def test_approval_not_required_auto_approves():
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    telegram = MagicMock()
    telegram.send_message_with_keyboard = AsyncMock()
    store = FakeStore()
    policy = make_policy()
    policy.approval_policy.approval_required = False
    skill = SignalApprovalGate(policy, telegram, store)
    result = await skill.run(make_ctx())
    assert result.status == "success"
    telegram.send_message_with_keyboard.assert_not_called()


async def test_dry_run_suppresses_telegram_returns_skip():
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    telegram = MagicMock()
    telegram.send_message_with_keyboard = AsyncMock()
    store = FakeStore()
    skill = SignalApprovalGate(make_policy(dry_run=True, dry_run_auto_approve=False), telegram, store)
    result = await skill.run(make_ctx())
    assert result.status == "skip"
    assert "dry_run" in result.reason
    telegram.send_message_with_keyboard.assert_not_called()


async def test_dry_run_auto_approve_returns_approved_simulated():
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    telegram = MagicMock()
    telegram.send_message_with_keyboard = AsyncMock()
    store = FakeStore()
    skill = SignalApprovalGate(make_policy(dry_run=True, dry_run_auto_approve=True), telegram, store)
    result = await skill.run(make_ctx())
    assert result.status == "success"
    assert result.updates["approval_status"] == "approved_simulated"
    telegram.send_message_with_keyboard.assert_not_called()
    assert store.calls[0][1] == "approved_simulated"
```

- [ ] **Step 2: Run — expect failure**

```bash
pytest tests/unit/test_signal_approval_gate.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Create package marker and implement**

```bash
touch ~/dev/trading-agent/skills/rollout/__init__.py
```

Create `skills/rollout/signal_approval_gate.py`:

```python
from __future__ import annotations
import html as _html
import logging
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel
from infra.storage.parsed_signal_store import ParsedSignalStore
from infra.telegram.client import TelegramClient

logger = logging.getLogger(__name__)
_AUTO_CLOSE_TYPES = frozenset({"CLOSE_SIGNAL", "PARTIAL_CLOSE"})


class SignalApprovalGate(Skill):
    name = "signal_approval_gate"

    def __init__(
        self,
        policy: PolicyModel,
        telegram: TelegramClient,
        store: ParsedSignalStore,
    ) -> None:
        self._policy = policy
        self._telegram = telegram
        self._store = store

    async def run(self, ctx: Context) -> SkillResult:
        signal_id = ctx.get("parsed_signal_id", "")
        signal_type = ctx.get("signal_type", "")
        ap = self._policy.approval_policy
        harness = self._policy.harness

        if harness.dry_run:
            msg = _format_approval_message(ctx)
            logger.info("DRY RUN approval suppressed:\n%s", msg)
            if harness.dry_run_auto_approve:
                await self._store.update_approval(signal_id, "approved_simulated", _now(), None)
                return SkillResult(status="success", updates={"approval_status": "approved_simulated"})
            return SkillResult(status="skip", reason="dry_run: approval suppressed")

        # Auto-approve closes if policy says so
        if ap.auto_approve_closes and signal_type in _AUTO_CLOSE_TYPES:
            await self._store.update_approval(signal_id, "approved", _now(), None)
            return SkillResult(status="success", updates={"approval_status": "approved"})

        # Auto-approve if approval gate is disabled
        if not ap.approval_required:
            await self._store.update_approval(signal_id, "approved", _now(), None)
            return SkillResult(status="success", updates={"approval_status": "approved"})

        # Send keyboard message and wait
        text = _format_approval_message(ctx)
        buttons = [[
            {"text": "✅ Approve", "callback_data": "approved"},
            {"text": "❌ Reject", "callback_data": "rejected"},
        ]]
        message_id = await self._telegram.send_message_with_keyboard(text, buttons)
        outcome = await self._telegram.wait_for_callback(message_id, ap.approval_timeout_secs)

        approved_at = _now() if outcome == "approved" else None
        await self._store.update_approval(signal_id, outcome, approved_at, message_id)

        if outcome == "approved":
            return SkillResult(status="success", updates={"approval_status": "approved"})
        if outcome == "rejected":
            return SkillResult(status="skip", reason="operator rejected signal")
        return SkillResult(
            status="skip",
            reason=f"approval timeout after {ap.approval_timeout_secs}s",
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_approval_message(ctx: Context) -> str:
    signal_type = _html.escape(ctx.get("signal_type", "?"))
    ticker = _html.escape(ctx.get("ticker") or "unresolved")
    author = _html.escape(ctx.get("author", "?"))
    channel = _html.escape(ctx.get("channel", "?"))
    conviction = _html.escape(ctx.get("conviction_bucket", "?"))
    pct = ctx.get("target_allocation_pct", 0.0) * 100
    raw = _html.escape(str(ctx.get("full_message_text", "?")))[:200]
    return (
        f"<b>SIGNAL — APPROVAL REQUIRED</b>\n\n"
        f"#{channel} · {author}\n"
        f"Intent: <b>{signal_type}</b>\n"
        f"Ticker: <b>{ticker}</b>\n"
        f"Conviction: {conviction} → {pct:.0f}% allocation\n\n"
        f"<i>{raw}</i>\n\n"
        f"<code>trace: {ctx.trace_id}</code>"
    )
```

- [ ] **Step 4: Run — expect pass**

```bash
pytest tests/unit/test_signal_approval_gate.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/rollout/__init__.py skills/rollout/signal_approval_gate.py tests/unit/test_signal_approval_gate.py
git commit -m "feat(skills): add SignalApprovalGate with Telegram inline keyboard and dry-run support"
```

---

## Task 10: Registry and main.py

**Files:**
- Modify: `agent/registry.py`
- Modify: `main.py`

- [ ] **Step 1: Add `build_phase2a_signal_chain` to `agent/registry.py`**

Open `agent/registry.py`. Add a new function after `build_phase1_chain`:

```python
def build_phase2a_signal_chain(
    policy,
    idempotency_store,
    parsed_signal_store,
    telegram_client,
) -> list:
    """Phase 2a signal chain: AX capture + full lifecycle intents + ParsedTradeSignal + approval gate + market hours."""
    from skills.signal.message_normalizer import MessageNormalizer
    from skills.signal.trade_signal_extractor import TradeSignalExtractor
    from skills.risk.idempotency_check import IdempotencyCheck
    from skills.signal.ticker_resolver import TickerResolver
    from skills.signal.conviction_classifier import ConvictionClassifier
    from skills.domain.parsed_signal_writer import ParsedSignalWriter
    from skills.domain.signal_disposition_resolver import SignalDispositionResolver
    from skills.rollout.signal_approval_gate import SignalApprovalGate
    from skills.risk.market_hours_guard import MarketHoursGuard

    return [
        MessageNormalizer(policy),
        TradeSignalExtractor(policy),
        IdempotencyCheck(policy, idempotency_store),
        TickerResolver(policy),
        ConvictionClassifier(policy),
        ParsedSignalWriter(parsed_signal_store),
        SignalDispositionResolver(),
        SignalApprovalGate(policy, telegram_client, parsed_signal_store),
        MarketHoursGuard(policy),
    ]
```

- [ ] **Step 2: Update `main.py` to use phase2a chain**

Replace the `run` function in `main.py` with:

```python
async def run(socket_path: str, db_path: str, policy_path: str) -> None:
    policy = load_policy(policy_path)
    conn = await get_connection(db_path)

    signal_store = SignalStore(conn)
    trace_store = TraceStore(conn)
    idempotency_store = IdempotencyStore(conn)

    from infra.storage.parsed_signal_store import ParsedSignalStore
    from agent.registry import build_phase2a_signal_chain
    parsed_signal_store = ParsedSignalStore(conn)

    telegram = TelegramClient(
        bot_token=policy.telegram.bot_token,
        chat_id=policy.telegram.chat_id,
    )

    chain = build_phase2a_signal_chain(policy, idempotency_store, parsed_signal_store, telegram)

    async def on_fail(ctx: Context, reason: str) -> None:
        if policy.harness.dry_run:
            logger.info("DRY RUN error digest suppressed: %s", reason)
            return
        import html
        text = (
            f"<b>ERROR</b>\n\n"
            f"Reason: {html.escape(reason)}\n"
            f"Channel: #{html.escape(ctx.get('channel', '?'))}\n"
            f"Preview: <i>{html.escape(ctx.get('trigger_preview', '?'))}</i>\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await telegram.send_message(text)
        except Exception as exc:
            logger.error("Error digest delivery failed: %s", exc)

    async def on_skip(ctx: Context, reason: str) -> None:
        if policy.harness.dry_run:
            logger.info("DRY RUN skip digest suppressed: %s", reason)

    orch = Orchestrator(chain, trace_store, on_skip=on_skip, on_fail=on_fail)

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
            "source": event.source,
            "trigger_preview": event.trigger_preview,
            "full_message_text": event.trigger_preview,
            "channel": event.channel,
            "author": event.author,
            "received_at": event.received_at,
        })

        await orch.run(ctx)

    reader = SocketReader(socket_path)
    logger.info("Trading agent Phase 2a ready. Listening on %s", socket_path)
    try:
        await reader.start(handle_event)
    finally:
        await conn.close()
```

- [ ] **Step 3: Verify import chain**

```bash
cd ~/dev/trading-agent
python3 -c "from agent.registry import build_phase2a_signal_chain; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add agent/registry.py main.py
git commit -m "feat(registry): add phase2a signal chain + wire main.py"
```

---

## Task 11: E2E test — phase2a signal pipeline

**Files:**
- Create: `tests/e2e/test_phase2a_signal_pipeline.py`

- [ ] **Step 1: Write the E2E tests**

Create `tests/e2e/test_phase2a_signal_pipeline.py`:

```python
import pytest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.policy import PolicyModel
from infra.storage.idempotency_store import IdempotencyStore
from infra.storage.trace_store import TraceStore
from infra.storage.parsed_signal_store import ParsedSignalStore
from agent.registry import build_phase2a_signal_chain
import yaml


def make_policy() -> PolicyModel:
    return PolicyModel.model_validate(
        yaml.safe_load((Path(__file__).parents[2] / "config" / "policy.yaml").read_text())
    )


def claude_resp(payload: dict) -> MagicMock:
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps(payload))]
    return m


def make_ctx(preview: str = "Long $AVEX today, initiating a position with high conviction") -> Context:
    ctx = Context(trace_id="trace-e2e-2a", event_id="evt-2a-1")
    ctx.update({
        "source": "injected",
        "trigger_preview": preview,
        "full_message_text": preview,
        "channel": "mystic",
        "author": "Mystic",
        "received_at": "2026-04-19T10:00:00Z",
    })
    return ctx


def build_chain_and_stores(db, telegram):
    policy = make_policy()
    idempotency_store = IdempotencyStore(db)
    parsed_signal_store = ParsedSignalStore(db)
    chain = build_phase2a_signal_chain(policy, idempotency_store, parsed_signal_store, telegram)
    return chain, parsed_signal_store, TraceStore(db)


# chain indices: 0=MessageNormalizer, 1=TradeSignalExtractor, 2=IdempotencyCheck,
#               3=TickerResolver, 4=ConvictionClassifier, 5=ParsedSignalWriter,
#               6=SignalDispositionResolver, 7=SignalApprovalGate


async def test_long_signal_approved_end_to_end(db, telegram):
    chain, parsed_signal_store, trace_store = build_chain_and_stores(db, telegram)

    telegram.send_message_with_keyboard = AsyncMock(return_value=42)
    telegram.wait_for_callback = AsyncMock(return_value="approved")

    with patch.object(chain[1]._client.messages, "create",
                      AsyncMock(return_value=claude_resp({"signal_type": "LONG_SIGNAL", "confidence": "high", "reason": "explicit long"}))):
        with patch.object(chain[3]._client.messages, "create",
                          AsyncMock(return_value=claude_resp({"ticker": "AVEX", "ambiguous": False, "asset_type_hint": "option"}))):
            with patch.object(chain[4]._client.messages, "create",
                              AsyncMock(return_value=claude_resp({"conviction_bucket": "high", "reason": "high conv"}))):
                orch = Orchestrator(chain, trace_store)
                ctx = make_ctx()
                await orch.run(ctx)

    # Approval gate was called
    telegram.send_message_with_keyboard.assert_called_once()
    # ParsedSignal was written with approved status
    rows = await db.execute_fetchall("SELECT * FROM parsed_signals")
    assert len(rows) == 1
    assert rows[0]["signal_type"] == "LONG_SIGNAL"
    assert rows[0]["ticker"] == "AVEX"
    assert rows[0]["approval_status"] == "approved"


async def test_close_signal_auto_approved_no_keyboard(db, telegram):
    chain, parsed_signal_store, trace_store = build_chain_and_stores(db, telegram)
    policy = make_policy()
    policy.approval_policy.auto_approve_closes = True

    telegram.send_message_with_keyboard = AsyncMock()

    with patch.object(chain[1]._client.messages, "create",
                      AsyncMock(return_value=claude_resp({"signal_type": "CLOSE_SIGNAL", "confidence": "high", "reason": "out"}))):
        with patch.object(chain[3]._client.messages, "create",
                          AsyncMock(return_value=claude_resp({"ticker": "AVEX", "ambiguous": False, "asset_type_hint": "option"}))):
            with patch.object(chain[4]._client.messages, "create",
                              AsyncMock(return_value=claude_resp({"conviction_bucket": "low", "reason": "close"}))):
                orch = Orchestrator(chain, trace_store)
                await orch.run(make_ctx("Out of AVEX, took profits"))

    telegram.send_message_with_keyboard.assert_not_called()
    rows = await db.execute_fetchall("SELECT * FROM parsed_signals")
    assert rows[0]["approval_status"] == "approved"
    assert rows[0]["signal_type"] == "CLOSE_SIGNAL"


async def test_watchlist_skips_no_parsed_signal(db, telegram):
    chain, parsed_signal_store, trace_store = build_chain_and_stores(db, telegram)

    with patch.object(chain[1]._client.messages, "create",
                      AsyncMock(return_value=claude_resp({"signal_type": "WATCHLIST_ONLY", "confidence": "high", "reason": "just watching"}))):
        orch = Orchestrator(chain, trace_store)
        await orch.run(make_ctx("Watching AVEX closely"))

    rows = await db.execute_fetchall("SELECT * FROM parsed_signals")
    assert len(rows) == 0  # skipped before ParsedSignalWriter


async def test_operator_reject_skips_no_approved(db, telegram):
    chain, parsed_signal_store, trace_store = build_chain_and_stores(db, telegram)

    telegram.send_message_with_keyboard = AsyncMock(return_value=99)
    telegram.wait_for_callback = AsyncMock(return_value="rejected")

    with patch.object(chain[1]._client.messages, "create",
                      AsyncMock(return_value=claude_resp({"signal_type": "LONG_SIGNAL", "confidence": "high", "reason": "long"}))):
        with patch.object(chain[3]._client.messages, "create",
                          AsyncMock(return_value=claude_resp({"ticker": "MSFT", "ambiguous": False, "asset_type_hint": "equity"}))):
            with patch.object(chain[4]._client.messages, "create",
                              AsyncMock(return_value=claude_resp({"conviction_bucket": "low", "reason": "low"}))):
                orch = Orchestrator(chain, trace_store)
                await orch.run(make_ctx("Long $MSFT here"))

    rows = await db.execute_fetchall("SELECT * FROM parsed_signals")
    assert rows[0]["approval_status"] == "rejected"


async def test_duplicate_signal_idempotent(db, telegram):
    chain, parsed_signal_store, trace_store = build_chain_and_stores(db, telegram)

    telegram.send_message_with_keyboard = AsyncMock(return_value=42)
    telegram.wait_for_callback = AsyncMock(return_value="approved")

    preview = "Long $AVEX, starting a position"
    with patch.object(chain[1]._client.messages, "create",
                      AsyncMock(return_value=claude_resp({"signal_type": "LONG_SIGNAL", "confidence": "high", "reason": "long"}))):
        with patch.object(chain[3]._client.messages, "create",
                          AsyncMock(return_value=claude_resp({"ticker": "AVEX", "ambiguous": False, "asset_type_hint": "option"}))):
            with patch.object(chain[4]._client.messages, "create",
                              AsyncMock(return_value=claude_resp({"conviction_bucket": "low", "reason": "low"}))):
                orch = Orchestrator(chain, trace_store)
                await orch.run(make_ctx(preview))   # first run
                await orch.run(make_ctx(preview))   # duplicate — same fingerprint

    # Only one keyboard message sent — second run skipped at IdempotencyCheck
    assert telegram.send_message_with_keyboard.call_count == 1
    rows = await db.execute_fetchall("SELECT * FROM parsed_signals")
    assert len(rows) == 1
```

- [ ] **Step 2: Add `execute_fetchall` helper to conftest.py**

Open `tests/conftest.py`. Add:

```python
import aiosqlite as _aiosqlite

@pytest.fixture
async def db():
    async with _aiosqlite.connect(":memory:") as conn:
        conn.row_factory = _aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        # Helper for test assertions
        async def execute_fetchall(sql, params=()):
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        conn.execute_fetchall = execute_fetchall
        yield conn
```

- [ ] **Step 3: Run E2E tests**

```bash
pytest tests/e2e/test_phase2a_signal_pipeline.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 4: Run full test suite to check no regressions**

```bash
pytest -v
```

Expected: all tests PASS. (Phase 1 E2E test uses `DesktopReader` — it may fail now that we've changed imports. If it does, that's expected — see Task 12.)

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_phase2a_signal_pipeline.py tests/conftest.py
git commit -m "test(e2e): add phase2a signal pipeline tests"
```

---

## Task 12: Delete removed files

**Files:**
- Delete: `skills/signal/desktop_reader.py`
- Delete: `tests/unit/test_desktop_reader.py`
- Modify: `tests/e2e/test_phase1_pipeline.py` — remove DesktopReader references

- [ ] **Step 1: Delete desktop_reader**

```bash
cd ~/dev/trading-agent
git rm skills/signal/desktop_reader.py tests/unit/test_desktop_reader.py
```

- [ ] **Step 2: Remove DesktopReader from phase1 E2E test**

Open `tests/e2e/test_phase1_pipeline.py`. Remove the `DesktopReader` import and its chain position (index 1). Update chain indices for `TradeIntentDetector` (was `chain[2]`, becomes `chain[1]`), `TickerResolver` (was `chain[4]`, becomes `chain[3]`), `ConvictionClassifier` (was `chain[5]`, becomes `chain[4]`).

The updated `build_chain` function:

```python
def build_chain(policy, idempotency_store, telegram):
    digest = TelegramDigest(telegram, mode="signal_only")
    chain = [
        MessageNormalizer(policy),
        TradeIntentDetector(policy),          # index 1 (was 2)
        IdempotencyCheck(policy, idempotency_store),
        TickerResolver(policy),               # index 3 (was 4)
        ConvictionClassifier(policy),         # index 4 (was 5)
        digest,
    ]
    return chain, digest
```

Also remove the `DesktopReader` import line and update all `chain[N]` references in the test body accordingly.

- [ ] **Step 3: Run full suite**

```bash
pytest -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_phase1_pipeline.py
git commit -m "chore: remove DesktopReader and update phase1 test chain indices"
```

---

## Task 13: AX Bridge — AXDiscordWatcher.swift

**Files:**
- Create: `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift`
- Modify: `bridge/Sources/NotificationBridge/main.swift`
- Delete: `bridge/Sources/NotificationBridge/NotificationPoller.swift`

This task requires macOS and Xcode command-line tools. Test with Accessibility Inspector.app before building.

- [ ] **Step 1: Verify Accessibility Inspector.app is available**

```bash
open -a "Accessibility Inspector"
```

If it opens: use it to inspect Discord's AX tree. Look for the message scroll area and note the AXRole of message container elements. This informs whether to register `kAXValueChangedNotification` or `kAXFocusedUIElementChangedNotification`.

- [ ] **Step 2: Delete NotificationPoller.swift**

```bash
cd ~/dev/trading-agent
git rm bridge/Sources/NotificationBridge/NotificationPoller.swift
```

- [ ] **Step 3: Create AXDiscordWatcher.swift**

Create `bridge/Sources/NotificationBridge/AXDiscordWatcher.swift`:

```swift
import AppKit
import ApplicationServices
import Foundation

/// Dual-mode Discord AX bridge.
/// Event mode: AXObserver callbacks on kAXValueChangedNotification.
/// Reconciliation mode: 5-second sweep of visible message pane.
final class AXDiscordWatcher {
    private let bundleId: String
    private let watchedChannels: Set<String>
    private let emitter: SocketEmitter
    private let logPath: String

    // Ring buffer — dedup across both modes
    private var seenFingerprints: [String] = []
    private let maxSeen = 200

    init(bundleId: String, watchedChannels: [String], socketPath: String, logPath: String) {
        self.bundleId = bundleId
        self.watchedChannels = Set(watchedChannels.map { $0.lowercased() })
        self.emitter = SocketEmitter(socketPath: socketPath)
        self.logPath = logPath
    }

    func start() {
        // Require Accessibility permission
        let opts = [kAXTrustedCheckOptionPrompt.takeRetainedValue() as String: true] as CFDictionary
        guard AXIsProcessTrustedWithOptions(opts) else {
            fputs("FATAL: Accessibility permission not granted.\n"
                + "Enable: System Settings → Privacy & Security → Accessibility → grant this terminal/app.\n", stderr)
            Foundation.exit(1)
        }

        guard let app = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleId).first else {
            fputs("Discord (\(bundleId)) is not running. Start Discord then retry.\n", stderr)
            Foundation.exit(1)
        }

        let pid = app.processIdentifier
        let axApp = AXUIElementCreateApplication(pid)

        // Create AXObserver
        var obs: AXObserver?
        let selfRef = Unmanaged.passRetained(self).toOpaque()
        let createResult = AXObserverCreate(pid, { _, element, notification, refcon in
            guard let refcon else { return }
            let w = Unmanaged<AXDiscordWatcher>.fromOpaque(refcon).takeUnretainedValue()
            w.handleCallback(element: element, notification: notification as String)
        }, &obs)

        guard createResult == .success, let obs else {
            fputs("AXObserverCreate failed: \(createResult.rawValue)\n", stderr)
            Foundation.exit(1)
        }

        // Register kAXValueChangedNotification — primary
        let valueResult = AXObserverAddNotification(
            obs, axApp, kAXValueChangedNotification as CFString, selfRef)
        switch valueResult {
        case .success:
            print("Registered kAXValueChangedNotification")
        case .notificationUnsupported:
            fputs("kAXValueChangedNotification unsupported — reconciliation mode only\n", stderr)
        default:
            fputs("AXObserverAddNotification (value) returned: \(valueResult.rawValue)\n", stderr)
        }

        // Also register focus changed as secondary source
        let focusResult = AXObserverAddNotification(
            obs, axApp, kAXFocusedUIElementChangedNotification as CFString, selfRef)
        if focusResult == .success {
            print("Registered kAXFocusedUIElementChangedNotification (secondary)")
        }

        CFRunLoopAddSource(CFRunLoopGetCurrent(), AXObserverGetRunLoopSource(obs), .defaultMode)

        // Reconciliation timer — 5-second sweep
        Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.reconcile(axApp: axApp)
        }

        print("AXDiscordWatcher running. bundle=\(bundleId) channels=\(watchedChannels.sorted())")
        CFRunLoopRun()  // blocks
    }

    // MARK: - Event mode

    private func handleCallback(element: AXUIElement, notification: String) {
        let logEntry: [String: String] = [
            "type": "ax_callback",
            "notification": notification,
            "ts": isoNow(),
        ]
        appendLog(logEntry)

        guard let msg = extractMessage(from: element) else { return }
        guard watchedChannels.contains(msg.channel) else { return }
        let fp = fingerprint(channel: msg.channel, author: msg.author, body: msg.body)
        guard markSeen(fp) else { return }
        emit(channel: msg.channel, author: msg.author, body: msg.body, source: "ax_event")
    }

    // MARK: - Reconciliation mode

    private func reconcile(axApp: AXUIElement) {
        var seen = 0
        walkTree(axApp, depth: 0, maxDepth: 8) { element in
            guard seen < 10 else { return }
            let role = axRole(of: element) ?? ""
            guard role == (kAXStaticTextRole as String) || role == (kAXTextAreaRole as String) else { return }
            let body = stringValue(of: element) ?? ""
            guard body.count > 25 else { return }
            let ch = activeChannel() ?? ""
            guard self.watchedChannels.contains(ch) else { return }
            let fp = self.fingerprint(channel: ch, author: "reconcile", body: body)
            guard self.markSeen(fp) else { return }
            self.emit(channel: ch, author: "reconcile", body: body, source: "reconciliation")
            seen += 1
        }
    }

    // MARK: - AX tree helpers

    private struct Message {
        let channel: String; let author: String; let body: String
    }

    private func extractMessage(from element: AXUIElement) -> Message? {
        let body = stringValue(of: element) ?? ""
        guard body.count > 25 else { return nil }
        let ch = activeChannel() ?? ""
        let author = authorFromParent(of: element) ?? "unknown"
        return Message(channel: ch.lowercased(), author: author, body: body)
    }

    private func activeChannel() -> String? {
        guard let app = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleId).first else { return nil }
        let axApp2 = AXUIElementCreateApplication(app.processIdentifier)
        var found: String?
        walkTree(axApp2, depth: 0, maxDepth: 5) { el in
            guard found == nil else { return }
            guard (axAttribute(of: el, key: kAXSelectedAttribute as CFString) as? Bool) == true else { return }
            let role = axRole(of: el) ?? ""
            guard role == (kAXButtonRole as String) || role == (kAXCellRole as String) else { return }
            if let title = stringValue(of: el), !title.isEmpty {
                let clean = title.hasPrefix("#") ? String(title.dropFirst()) : title
                found = clean.lowercased()
            }
        }
        return found
    }

    private func authorFromParent(of element: AXUIElement) -> String? {
        var parent: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXParentAttribute as CFString, &parent) == .success else { return nil }
        let parentEl = parent as! AXUIElement
        var children: CFTypeRef?
        guard AXUIElementCopyAttributeValue(parentEl, kAXChildrenAttribute as CFString, &children) == .success,
              let kids = children as? [AXUIElement] else { return nil }
        return kids.compactMap { stringValue(of: $0) }.first
    }

    private func walkTree(_ element: AXUIElement, depth: Int, maxDepth: Int, visitor: (AXUIElement) -> Void) {
        guard depth <= maxDepth else { return }
        visitor(element)
        var children: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success,
              let kids = children as? [AXUIElement] else { return }
        for kid in kids { walkTree(kid, depth: depth + 1, maxDepth: maxDepth, visitor: visitor) }
    }

    private func axRole(of element: AXUIElement) -> String? {
        axAttribute(of: element, key: kAXRoleAttribute as CFString) as? String
    }

    private func axAttribute(of element: AXUIElement, key: CFString) -> Any? {
        var val: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, key, &val) == .success else { return nil }
        return val
    }

    private func stringValue(of element: AXUIElement) -> String? {
        (axAttribute(of: element, key: kAXValueAttribute as CFString) as? String)
            ?? (axAttribute(of: element, key: kAXTitleAttribute as CFString) as? String)
    }

    // MARK: - Dedup

    private func fingerprint(channel: String, author: String, body: String) -> String {
        let input = "\(channel):\(author):\(body.prefix(120))"
        var h: UInt64 = 5381
        for b in input.utf8 { h = h &* 31 &+ UInt64(b) }
        return String(format: "%016llx", h)
    }

    private func markSeen(_ fp: String) -> Bool {
        guard !seenFingerprints.contains(fp) else { return false }
        seenFingerprints.append(fp)
        if seenFingerprints.count > maxSeen { seenFingerprints.removeFirst() }
        return true
    }

    // MARK: - Emit + Log

    private func emit(channel: String, author: String, body: String, source: String) {
        let event: [String: String] = [
            "event_id": UUID().uuidString,
            "source": source,
            "channel": channel,
            "author": author,
            "trigger_preview": body,
            "received_at": isoNow(),
        ]
        emitter.emit(event)
        print("[\(source)] #\(channel) \(author): \(body.prefix(60))")
    }

    private func appendLog(_ entry: [String: String]) {
        guard let data = try? JSONSerialization.data(withJSONObject: entry),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        let url = URL(fileURLWithPath: logPath)
        if let fh = try? FileHandle(forWritingTo: url) {
            fh.seekToEndOfFile()
            fh.write(line.data(using: .utf8)!)
            try? fh.close()
        } else {
            try? line.data(using: .utf8)!.write(to: url)
        }
    }

    private func isoNow() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}
```

- [ ] **Step 4: Update `bridge/Sources/NotificationBridge/main.swift`**

Replace the full contents of `bridge/Sources/NotificationBridge/main.swift`:

```swift
import Foundation

let socketPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1] : "/tmp/trading_bridge.sock"
let logPath = CommandLine.arguments.count > 2
    ? CommandLine.arguments[2] : "data/ax_events.log"

// Ensure data directory exists for log
let dataDir = URL(fileURLWithPath: logPath).deletingLastPathComponent().path
try? FileManager.default.createDirectory(atPath: dataDir, withIntermediateDirectories: true)

let watcher = AXDiscordWatcher(
    bundleId: "com.hnc.Discord",
    watchedChannels: ["mystic", "alerts", "trades"],
    socketPath: socketPath,
    logPath: logPath
)
watcher.start()   // blocks on CFRunLoopRun
```

- [ ] **Step 5: Build the Swift bridge**

```bash
cd ~/dev/trading-agent/bridge
swift build -c release 2>&1
```

Expected: compilation succeeds. If Discord's AX tree doesn't expose `kAXValueChangedNotification`, you'll see the warning at runtime (not compile time) — that's expected and handled.

- [ ] **Step 6: Manual test — inject a fake event via inject_event.py**

In terminal 1 — start the Python agent (approval will timeout since Telegram isn't configured):
```bash
cd ~/dev/trading-agent
python3 main.py --socket /tmp/trading_bridge.sock --db data/test.db
```

In terminal 2 — inject a test event:
```bash
python3 inject_event.py "Long \$AVEX today IPO high conviction" --channel mystic --author TestUser
```

Expected in terminal 1: `Received event ... from #mystic by TestUser` and then the pipeline runs (TradeSignalExtractor calls Claude, etc.).

- [ ] **Step 7: Manual test — live AX bridge**

Start the compiled bridge:
```bash
./bridge/.build/release/NotificationBridge /tmp/trading_bridge.sock data/ax_events.log
```

While the Python agent is running, send a Discord message in a watched channel. Expected: event emitted, pipeline runs.

Check `data/ax_events.log` for logged AX callbacks.

- [ ] **Step 8: Commit**

```bash
git add bridge/Sources/NotificationBridge/AXDiscordWatcher.swift \
        bridge/Sources/NotificationBridge/main.swift
git commit -m "feat(bridge): replace NotificationPoller with AXDiscordWatcher (dual-mode)"
```

---

## Task 15: ~~HarnessPolicy — dry-run flag~~

> **Note:** `HarnessPolicy`, dry-run behavior in `SignalApprovalGate`, and `on_fail`/`on_skip` dry-run guards in `main.py` are now fully implemented in Tasks 4, 9, and 10 respectively. Task 15 is superseded and requires no additional steps.

---

## Task 16: MarketHoursGuard skill

**Prerequisite:** Task 10 (`build_phase2a_signal_chain` already imports and appends `MarketHoursGuard`).

**Files:**
- Create: `skills/risk/market_hours_guard.py`
- Create: `tests/unit/test_market_hours_guard.py`

> **Note:** The registry change (inserting `MarketHoursGuard` into `build_phase2a_signal_chain`) is already in Task 10 Step 1. This task only creates the skill file and its tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_market_hours_guard.py`:

```python
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from agent.context import Context
from agent.policy import PolicyModel
import yaml

_ET = ZoneInfo("America/New_York")


def make_policy(**market_hours_overrides) -> PolicyModel:
    raw = yaml.safe_load((Path(__file__).parents[2] / "config" / "policy.yaml").read_text())
    raw["market_hours"].update(market_hours_overrides)
    return PolicyModel.model_validate(raw)


def make_ctx(asset_type: str = "option") -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"asset_type_hint": asset_type})
    return ctx


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 4, 20, hour, minute, tzinfo=_ET)


async def test_option_in_rth():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(make_policy(), time_fn=lambda: at(10, 0))
    result = await skill.run(make_ctx("option"))
    assert result.status == "success"


async def test_option_outside_rth():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(make_policy(), time_fn=lambda: at(17, 0))
    result = await skill.run(make_ctx("option"))
    assert result.status == "fail"
    assert "execution_ineligible" in result.reason
    assert "17:00" in result.reason


async def test_option_at_rth_open_boundary():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(make_policy(), time_fn=lambda: at(9, 30))
    result = await skill.run(make_ctx("option"))
    assert result.status == "success"


async def test_option_before_rth_open():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(make_policy(), time_fn=lambda: at(9, 29))
    result = await skill.run(make_ctx("option"))
    assert result.status == "fail"
    assert "execution_ineligible" in result.reason


async def test_equity_premarket_allowed():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(make_policy(), time_fn=lambda: at(6, 0))
    result = await skill.run(make_ctx("equity"))
    assert result.status == "success"


async def test_equity_before_premarket_window():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(make_policy(), time_fn=lambda: at(3, 59))
    result = await skill.run(make_ctx("equity"))
    assert result.status == "fail"
    assert "execution_ineligible" in result.reason


async def test_equity_afterhours_queue_enabled():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(make_policy(), time_fn=lambda: at(17, 0))
    result = await skill.run(make_ctx("equity"))
    assert result.status == "success"
    assert result.updates.get("queued") is True


async def test_equity_afterhours_queue_disabled():
    from skills.risk.market_hours_guard import MarketHoursGuard
    skill = MarketHoursGuard(
        make_policy(stock_afterhours_queue=False),
        time_fn=lambda: at(17, 0),
    )
    result = await skill.run(make_ctx("equity"))
    assert result.status == "fail"
    assert "execution_ineligible" in result.reason
```

- [ ] **Step 2: Run — expect failure**

```bash
cd ~/dev/trading-agent
pytest tests/unit/test_market_hours_guard.py -v
```

Expected: `ImportError` — `market_hours_guard` module doesn't exist yet.

- [ ] **Step 3: Implement `skills/risk/market_hours_guard.py`**

Create `skills/risk/market_hours_guard.py`:

```python
from __future__ import annotations
from datetime import datetime, time
from typing import Callable
from zoneinfo import ZoneInfo
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel

_ET = ZoneInfo("America/New_York")


def _default_time_fn() -> datetime:
    return datetime.now(_ET)


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class MarketHoursGuard(Skill):
    name = "market_hours_guard"

    def __init__(self, policy: PolicyModel, time_fn: Callable[[], datetime] | None = None) -> None:
        self._policy = policy
        self._time_fn = time_fn or _default_time_fn

    async def run(self, ctx: Context) -> SkillResult:
        asset_type = ctx.get("asset_type_hint", "equity")
        mh = self._policy.market_hours
        now = self._time_fn()
        current = now.time()

        rth_start = _parse_time(mh.rth_start)
        rth_end = _parse_time(mh.rth_end)
        in_rth = rth_start <= current < rth_end

        if asset_type == "option":
            if in_rth:
                return SkillResult(status="success")
            return SkillResult(
                status="fail",
                reason=(
                    f"execution_ineligible: option outside RTH "
                    f"(current ET {current.strftime('%H:%M')}, "
                    f"allowed {mh.rth_start}–{mh.rth_end})"
                ),
            )

        # equity
        if in_rth:
            return SkillResult(status="success")

        premarket_start = _parse_time(mh.stock_premarket_start)
        if premarket_start <= current < rth_start:
            if mh.stock_premarket_allowed:
                return SkillResult(status="success")
            return SkillResult(
                status="fail",
                reason=(
                    f"execution_ineligible: equity premarket not allowed "
                    f"(current ET {current.strftime('%H:%M')})"
                ),
            )

        if current >= rth_end:
            if mh.stock_afterhours_queue:
                return SkillResult(status="success", updates={"queued": True})
            return SkillResult(
                status="fail",
                reason=(
                    f"execution_ineligible: equity afterhours queueing disabled "
                    f"(current ET {current.strftime('%H:%M')})"
                ),
            )

        return SkillResult(
            status="fail",
            reason=(
                f"execution_ineligible: equity market not open "
                f"(current ET {current.strftime('%H:%M')}, "
                f"premarket from {mh.stock_premarket_start})"
            ),
        )
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/unit/test_market_hours_guard.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Verify import chain**

```bash
cd ~/dev/trading-agent
python3 -c "from agent.registry import build_phase2a_signal_chain; print('ok')"
```

Expected: `ok`

- [ ] **Step 6: Run full test suite — no regressions**

```bash
pytest -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add skills/risk/market_hours_guard.py \
        tests/unit/test_market_hours_guard.py
git commit -m "feat(skills): add MarketHoursGuard — execution eligibility gate after approval"
```

---

## Phase 2a-signal Checklist

Before advancing to Phase 2a-execution (Plan B):

- [ ] No missed or duplicated captures in a 5-day observed window
- [ ] 30+ real signals parsed with correct lifecycle intent (verified in `parsed_signals` table)
- [ ] 95%+ ticker correctness on actionable signals (spot-check against Discord messages)
- [ ] 0 silent bridge failures without a logged entry in `data/ax_events.log`
- [ ] Reconciliation mode catches at least one event-mode miss (test by temporarily killing the AX observer callback and confirming the 5s sweep picks it up)
- [ ] Approval gate keyboard messages arrive within 3 seconds of signal capture
- [ ] `CLOSE_SIGNAL` auto-approves without keyboard when `auto_approve_closes: true`
- [ ] `AGENT_CONTRACT.md` exists at repo root and all LLM skill prompts cite it
- [ ] `dry_run: true` in policy suppresses all Telegram sends and returns appropriate skip/simulated outcomes
- [ ] Options signal outside RTH fails at `MarketHoursGuard` after approval message is sent

---

## Not covered here (Plan B — Phase 2a-execution)

- IBKR client wrapper (`infra/ibkr/client.py`)
- `ChainLookup`, `ContractSelector`, `LiquidityCheck`
- `OrderSizer`, `OrderPolicyGuard`, `ExecutionPlanWriter`
- `OrderSubmitter` (ib_insync)
- `PositionRegistryUpdater` + `positions` DB table
- `execution_plans` DB table
- `pyproject.toml` additions: `ib-insync`
- `RegimeCatalystUpgrader` (deferred: requires market-data + EMA infrastructure)
