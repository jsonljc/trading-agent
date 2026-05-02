# Per-Trader Conviction Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the global high/low conviction pipeline with per-trader YAML profiles, a single few-shot LLM classifier, and a two-bucket (5% / 10%) sizing model — all on the latency-critical hot path.

**Architecture:** A new `TraderRouter` skill resolves the message author to a trader profile loaded from `config/traders/*.yaml`. A new `TraderClassifier` skill performs feature extraction (regex), takes a deterministic shortcut when the message states a numeric size, and otherwise issues a single prompt-cached LLM call returning `{is_entry, ticker, side, bucket, confidence}`. `OrderSizer` reads `size_pct` directly from context. Every classification is written to `classification_log`. Bootstrap (`auto_execute: false`) traders post a review digest to Telegram instead of firing.

**Tech Stack:** Python 3.12, anthropic SDK (Haiku), aiosqlite, PyYAML (already in `policy.py`), pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-02-conviction-classification-design.md`

---

## File Structure

**New files:**
- `agent/traders/__init__.py`
- `agent/traders/profile.py` — `TraderProfile`, `ConvictionExample` dataclasses + YAML loader
- `agent/traders/registry.py` — In-memory registry of profiles keyed by author display name
- `skills/signal/feature_extractor.py` — Pure-function regex feature extraction
- `skills/signal/trader_router.py` — Resolves Discord author → `TraderProfile`; sets `ctx["trader"]`
- `skills/signal/trader_classifier.py` — Replaces `SignalAnalyzer` + `ConvictionClassifier` in one skill
- `infra/storage/classification_log_store.py` — Writes to `classification_log`
- `infra/storage/examples_pending_store.py` — Writes to `trader_examples_pending`
- `infra/storage/trader_state_store.py` — Reads/writes `trader_state` (availability)
- `config/traders/wallstengine.yaml`
- `config/traders/stocktalkweekly.yaml`
- `config/traders/mystic.yaml`
- `bin/promote_examples.py` — CLI: list pending examples and append approved ones to YAML
- `tests/unit/test_feature_extractor.py`
- `tests/unit/test_trader_profile_loader.py`
- `tests/unit/test_trader_router.py`
- `tests/unit/test_trader_classifier.py`
- `tests/integration/test_classification_log_store.py`
- `tests/integration/test_examples_pending_store.py`
- `tests/integration/test_trader_state_store.py`
- `tests/integration/test_pipeline_phase1_traders.py`

**Modified files:**
- `infra/storage/db.py` — add three new tables to `SCHEMA`
- `skills/execution/order_sizer.py` — read `size_pct` from ctx instead of `sizing_policy`
- `agent/registry.py` — replace `SignalAnalyzer` with `TraderRouter` + `TraderClassifier`; add bootstrap branch
- `skills/posttrade/telegram_digest.py` — add `send_bootstrap_review_digest`
- `tests/unit/test_order_sizer.py` — update for new size_pct flow

**Files deleted (Task 13 — clean cutover, the legacy pipeline was never run in production):**
- `skills/signal/signal_analyzer.py`
- `skills/signal/conviction_classifier.py`
- `tests/unit/test_signal_analyzer.py`
- `tests/unit/test_conviction_classifier.py`
- `agent/policy.py` — `SizingPolicy` class + `sizing_policy` field on `PolicyModel`
- `config/policy.yaml` — `sizing_policy:` block

---

## Task 1: Database schema — new tables

**Files:**
- Modify: `infra/storage/db.py`
- Test: `tests/integration/test_classification_log_store.py` (created in Task 7); for now we only verify schema applies cleanly.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_db_schema.py`:

```python
import pytest
import aiosqlite
from infra.storage.db import SCHEMA


@pytest.mark.asyncio
async def test_schema_creates_classification_log_and_pending_and_state():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(SCHEMA)
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('classification_log','trader_examples_pending','trader_state')"
        )
        rows = await cursor.fetchall()
        names = {r[0] for r in rows}
    assert names == {"classification_log", "trader_examples_pending", "trader_state"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_db_schema.py -v`
Expected: FAIL — tables don't exist yet.

- [ ] **Step 3: Add tables to `SCHEMA`**

In `infra/storage/db.py`, append to the `SCHEMA` string before the closing triple-quote:

```sql
CREATE TABLE IF NOT EXISTS classification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    trader_handle TEXT NOT NULL,
    msg_text TEXT NOT NULL,
    features_json TEXT NOT NULL,
    llm_response_json TEXT,
    bucket TEXT NOT NULL,
    confidence REAL NOT NULL,
    size_pct REAL NOT NULL,
    size_source TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_classification_log_trader_time
    ON classification_log(trader_handle, created_at);

CREATE TABLE IF NOT EXISTS trader_examples_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_handle TEXT NOT NULL,
    msg_text TEXT NOT NULL,
    proposed_bucket TEXT NOT NULL,
    proposed_why TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_bucket TEXT
);

CREATE TABLE IF NOT EXISTS trader_state (
    trader_handle TEXT PRIMARY KEY,
    unavailable_until TEXT,
    updated_at TEXT NOT NULL
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_db_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full unit test suite — schema change must not break anything**

Run: `pytest tests/unit -x -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add infra/storage/db.py tests/unit/test_db_schema.py
git commit -m "feat(db): add classification_log, trader_examples_pending, trader_state tables"
```

---

## Task 2: `TraderProfile` dataclass + YAML loader

**Files:**
- Create: `agent/traders/__init__.py`
- Create: `agent/traders/profile.py`
- Test: `tests/unit/test_trader_profile_loader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_trader_profile_loader.py`:

```python
import pytest
from pathlib import Path
from agent.traders.profile import TraderProfile, ConvictionExample, load_profile, load_all_profiles


YAML_TEXT = """
handle: testtrader
display_name: Test Trader
discord_author_pattern: "Test Trader"
alert_mention: "@Test - Alerts"
require_alert_mention: true
bot_authors_to_skip: []
auto_execute: true
size_in_message: false
prefer_message_size: true
classifier_model: claude-haiku-4-5
availability_phrases: ["off the grid"]
conviction_examples:
  - msg: "Added 2% TEST"
    bucket: LOW
    why: "explicit 2%"
  - msg: "watching TEST"
    bucket: SKIP
    why: "no entry"
"""


def test_load_profile_parses_all_fields(tmp_path: Path):
    p = tmp_path / "test.yaml"
    p.write_text(YAML_TEXT)
    profile = load_profile(p)
    assert profile.handle == "testtrader"
    assert profile.display_name == "Test Trader"
    assert profile.alert_mention == "@Test - Alerts"
    assert profile.require_alert_mention is True
    assert profile.auto_execute is True
    assert profile.classifier_model == "claude-haiku-4-5"
    assert profile.availability_phrases == ["off the grid"]
    assert len(profile.conviction_examples) == 2
    assert profile.conviction_examples[0] == ConvictionExample(
        msg="Added 2% TEST", bucket="LOW", why="explicit 2%"
    )


def test_load_profile_rejects_invalid_bucket(tmp_path: Path):
    bad = YAML_TEXT.replace("bucket: LOW", "bucket: BANANA")
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(ValueError, match="invalid bucket"):
        load_profile(p)


def test_load_all_profiles_reads_directory(tmp_path: Path):
    (tmp_path / "a.yaml").write_text(YAML_TEXT)
    (tmp_path / "b.yaml").write_text(YAML_TEXT.replace("testtrader", "second"))
    profiles = load_all_profiles(tmp_path)
    handles = {p.handle for p in profiles}
    assert handles == {"testtrader", "second"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_trader_profile_loader.py -v`
Expected: FAIL — `agent.traders.profile` does not exist.

- [ ] **Step 3: Create the package and the loader**

Create `agent/traders/__init__.py` (empty file).

Create `agent/traders/profile.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import yaml


VALID_BUCKETS = {"LOW", "HIGH", "SKIP"}


@dataclass(frozen=True)
class ConvictionExample:
    msg: str
    bucket: str
    why: str


@dataclass(frozen=True)
class TraderProfile:
    handle: str
    display_name: str
    discord_author_pattern: str
    alert_mention: str
    require_alert_mention: bool
    bot_authors_to_skip: list[str]
    auto_execute: bool
    size_in_message: bool
    prefer_message_size: bool
    classifier_model: str
    availability_phrases: list[str]
    conviction_examples: list[ConvictionExample]


def load_profile(path: Path) -> TraderProfile:
    raw = yaml.safe_load(path.read_text())
    examples = []
    for e in raw.get("conviction_examples", []):
        if e["bucket"] not in VALID_BUCKETS:
            raise ValueError(f"invalid bucket {e['bucket']!r} in {path}")
        examples.append(ConvictionExample(msg=e["msg"], bucket=e["bucket"], why=e.get("why", "")))
    return TraderProfile(
        handle=raw["handle"],
        display_name=raw["display_name"],
        discord_author_pattern=raw["discord_author_pattern"],
        alert_mention=raw["alert_mention"],
        require_alert_mention=bool(raw.get("require_alert_mention", True)),
        bot_authors_to_skip=list(raw.get("bot_authors_to_skip", [])),
        auto_execute=bool(raw.get("auto_execute", False)),
        size_in_message=bool(raw.get("size_in_message", False)),
        prefer_message_size=bool(raw.get("prefer_message_size", True)),
        classifier_model=raw.get("classifier_model", "claude-haiku-4-5"),
        availability_phrases=list(raw.get("availability_phrases", [])),
        conviction_examples=examples,
    )


def load_all_profiles(directory: Path) -> list[TraderProfile]:
    return [load_profile(p) for p in sorted(directory.glob("*.yaml"))]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_trader_profile_loader.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/traders/__init__.py agent/traders/profile.py tests/unit/test_trader_profile_loader.py
git commit -m "feat(traders): TraderProfile dataclass and YAML loader"
```

---

## Task 3: Seed YAML profiles for three traders

**Files:**
- Create: `config/traders/wallstengine.yaml`
- Create: `config/traders/stocktalkweekly.yaml`
- Create: `config/traders/mystic.yaml`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_seed_profiles.py`:

```python
from pathlib import Path
from agent.traders.profile import load_all_profiles, VALID_BUCKETS


REPO_ROOT = Path(__file__).resolve().parents[2]
TRADERS_DIR = REPO_ROOT / "config" / "traders"


def test_three_seed_profiles_load():
    profiles = load_all_profiles(TRADERS_DIR)
    handles = {p.handle for p in profiles}
    assert {"wallstengine", "stocktalkweekly", "mystic"}.issubset(handles)


def test_each_seed_profile_has_at_least_three_examples_with_valid_buckets():
    profiles = load_all_profiles(TRADERS_DIR)
    for p in profiles:
        assert len(p.conviction_examples) >= 3, f"{p.handle} has too few examples"
        for ex in p.conviction_examples:
            assert ex.bucket in VALID_BUCKETS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_seed_profiles.py -v`
Expected: FAIL — directory doesn't exist.

- [ ] **Step 3: Create `config/traders/wallstengine.yaml`**

```yaml
handle: wallstengine
display_name: Wall St Engine
discord_author_pattern: "Wall St Engine"
alert_mention: "@Wall - Alerts"
require_alert_mention: true
bot_authors_to_skip: ["WSE"]
auto_execute: true
size_in_message: true
prefer_message_size: true
classifier_model: claude-haiku-4-5
availability_phrases: []
conviction_examples:
  - msg: "Added small AUDC (speculative play) 2% pos. on back of BAND earnings"
    bucket: LOW
    why: "speculative play, small, 2%"
  - msg: "OPEN $SHEN — taking a stab at SHEN around 21DMA ahead of earnings Friday"
    bucket: LOW
    why: "stab/small horizon framing"
  - msg: "Added a 2% position in CEG calls. After a month of consolidation, CEG looks like it's setting up for a move here."
    bucket: LOW
    why: "2% explicit, standard add"
  - msg: "PORTFOLIO UPDATE - 18 POSITIONS — 94:6 EQUITY:OPTIONS RATIO ..."
    bucket: SKIP
    why: "portfolio recap, not an entry"
  - msg: "FDA APPROVES ARVINAS' $ARVN VEPDEGESTRANT"
    bucket: SKIP
    why: "WSE bot news headline"
```

- [ ] **Step 4: Create `config/traders/stocktalkweekly.yaml`**

```yaml
handle: stocktalkweekly
display_name: Stock Talk Weekly
discord_author_pattern: "Stock Talk Weekly"
alert_mention: "@Stock Talk Weekly - Alerts"
require_alert_mention: true
bot_authors_to_skip: []
auto_execute: false
size_in_message: true
prefer_message_size: true
classifier_model: claude-haiku-4-5
availability_phrases: ["off the grid", "passover", "on vacation"]
conviction_examples:
  - msg: "This will join the 'Prospective Positions' group with a small 1% weighting @ $41.22"
    bucket: LOW
    why: "explicit 1%, prospective starter"
  - msg: "names that I want to upsize when the opportunity arises"
    bucket: SKIP
    why: "intent to upsize later, not an entry now"
  - msg: "Fully closed remainder of position. Too much uncertainty around shipping insurance & fuel prices in this environment ... lowest conviction name in the portfolio"
    bucket: SKIP
    why: "exit, not entry"
  - msg: "PORTFOLIO UPDATE - 18 POSITIONS"
    bucket: SKIP
    why: "ledger snapshot"
  - msg: "Yet another intraday fade in the market with $SPY $QQQ flushing red. Market can't get off the mat with these oil concerns."
    bucket: SKIP
    why: "macro commentary"
```

- [ ] **Step 5: Create `config/traders/mystic.yaml`**

```yaml
handle: mystic
display_name: UndefinedMystic
discord_author_pattern: "UndefinedMystic"
alert_mention: "@Alerts - Mystic"
require_alert_mention: true
bot_authors_to_skip: []
auto_execute: false
size_in_message: false
prefer_message_size: true
classifier_model: claude-haiku-4-5
availability_phrases: ["off the grid", "passover", "on vacation", "traveling"]
conviction_examples:
  - msg: "i opened a small swing trade position in INDI — short term swing trade based on momentum in semis"
    bucket: LOW
    why: "small + swing trade self-label"
  - msg: "bought todays IPO $ELMT here at $17.90 for a swing trade as we've seen IPOs have been hot lately"
    bucket: LOW
    why: "swing trade framing, single-shot"
  - msg: "Alpha + Omega Semiconductor long idea. Been looking for the unique undiscovered way to get meaningful exposure to the agentic ai induced CPU boom. [multi-paragraph thesis]"
    bucket: HIGH
    why: "labeled long idea, deep multi-point thesis"
  - msg: "These are mid level conviction swing trade ideas for current mkt regime always do your own dd"
    bucket: LOW
    why: "self-stamped mid-level swing"
  - msg: "fyi.... INTTeresting given nxpi earnings move today"
    bucket: SKIP
    why: "fyi/interesting, no entry"
  - msg: "tailwind for the apple peice...."
    bucket: SKIP
    why: "color, not entry"
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/unit/test_seed_profiles.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add config/traders/ tests/unit/test_seed_profiles.py
git commit -m "feat(traders): seed YAML profiles for WSE, STW, Mystic with conviction examples"
```

---

## Task 4: Feature extractor (regex)

**Files:**
- Create: `skills/signal/feature_extractor.py`
- Test: `tests/unit/test_feature_extractor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_feature_extractor.py`:

```python
from skills.signal.feature_extractor import extract_features


def test_extracts_stated_size_pct_and_entry_verb():
    f = extract_features("Added a 2% position in CEG calls. Looking for a move.")
    assert f.stated_size_pct == 2.0
    assert f.entry_verb_present is True
    assert "CEG" in f.tickers_in_msg


def test_extracts_dollar_prefixed_tickers():
    f = extract_features("OPEN $SHEN @Wall - Alerts taking a stab")
    assert "SHEN" in f.tickers_in_msg
    assert f.entry_verb_present is True


def test_no_entry_verb_in_commentary():
    f = extract_features("watching TEST closely, no position yet")
    assert f.entry_verb_present is False
    assert f.stated_size_pct is None


def test_detects_availability_phrase():
    f = extract_features("will be off the grid for passover", availability_phrases=["off the grid", "passover"])
    assert f.availability_phrase == "off the grid"


def test_msg_length_and_thread_reply():
    msg = "looks ready"
    f = extract_features(msg, is_thread_reply=True)
    assert f.msg_length == len(msg)
    assert f.is_thread_reply is True


def test_size_capped_phrase_match_case_insensitive():
    f = extract_features("ADDED 5% pos AAPL")
    assert f.stated_size_pct == 5.0
    assert f.entry_verb_present is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_feature_extractor.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `feature_extractor.py`**

Create `skills/signal/feature_extractor.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field
import re


_ENTRY_VERBS = re.compile(
    r"\b(open|opening|opened|added|adding|bought|initiating|initiated|"
    r"joining|joined|loading|took|grabbed|picked up|started|scaled in)\b",
    re.IGNORECASE,
)
_SIZE_PCT = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:pos|position|weighting|wt)?",
    re.IGNORECASE,
)
_TICKERS = re.compile(r"\$?([A-Z]{1,6})\b")
_DOLLAR_TICKERS = re.compile(r"\$([A-Z]{1,6})\b")


@dataclass(frozen=True)
class Features:
    stated_size_pct: float | None
    entry_verb_present: bool
    tickers_in_msg: list[str]
    embed_present: bool
    msg_length: int
    is_thread_reply: bool
    availability_phrase: str | None = None


def extract_features(
    msg: str,
    *,
    is_thread_reply: bool = False,
    embed_present: bool = False,
    availability_phrases: list[str] | None = None,
) -> Features:
    size_match = _SIZE_PCT.search(msg)
    stated_size = float(size_match.group(1)) if size_match else None

    entry_verb = bool(_ENTRY_VERBS.search(msg))

    dollar_tickers = _DOLLAR_TICKERS.findall(msg)
    if dollar_tickers:
        tickers = list(dict.fromkeys(dollar_tickers))
    else:
        tickers = list(dict.fromkeys(t for t in _TICKERS.findall(msg) if t.isupper() and 1 <= len(t) <= 6))

    availability = None
    for phrase in availability_phrases or []:
        if phrase.lower() in msg.lower():
            availability = phrase
            break

    return Features(
        stated_size_pct=stated_size,
        entry_verb_present=entry_verb,
        tickers_in_msg=tickers,
        embed_present=embed_present,
        msg_length=len(msg),
        is_thread_reply=is_thread_reply,
        availability_phrase=availability,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_feature_extractor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/signal/feature_extractor.py tests/unit/test_feature_extractor.py
git commit -m "feat(signal): regex-based feature extractor for trader classifier"
```

---

## Task 5: `TraderRouter` skill — author → profile

**Files:**
- Create: `agent/traders/registry.py`
- Create: `skills/signal/trader_router.py`
- Test: `tests/unit/test_trader_router.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_trader_router.py`:

```python
import pytest
from agent.context import Context
from agent.traders.profile import TraderProfile, ConvictionExample
from agent.traders.registry import TraderRegistry
from skills.signal.trader_router import TraderRouter


def make_profile(handle: str, author: str, bot_skip: list[str] | None = None) -> TraderProfile:
    return TraderProfile(
        handle=handle, display_name=author, discord_author_pattern=author,
        alert_mention=f"@{author} - Alerts", require_alert_mention=True,
        bot_authors_to_skip=bot_skip or [], auto_execute=True,
        size_in_message=False, prefer_message_size=True,
        classifier_model="claude-haiku-4-5", availability_phrases=[],
        conviction_examples=[ConvictionExample(msg="x", bucket="LOW", why="y")],
    )


@pytest.mark.asyncio
async def test_router_attaches_matching_profile_to_ctx():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine")])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={"author": "Wall St Engine", "full_message_text": "OPEN $X"})

    result = await router.run(ctx)

    assert result.status == "success"
    assert ctx.get("trader_handle") == "wse"


@pytest.mark.asyncio
async def test_router_skips_when_no_matching_profile():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine")])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={"author": "Random Person", "full_message_text": "buy AAPL"})

    result = await router.run(ctx)

    assert result.status == "skip"
    assert "no_trader_profile" in (result.reason or "")


@pytest.mark.asyncio
async def test_router_skips_bot_authors():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine", bot_skip=["WSE"])])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={"author": "WSE", "full_message_text": "FDA APPROVES X"})

    result = await router.run(ctx)
    assert result.status == "skip"
    assert "bot_author" in (result.reason or "")


@pytest.mark.asyncio
async def test_router_skips_when_alert_mention_required_but_missing():
    registry = TraderRegistry([make_profile("wse", "Wall St Engine")])
    router = TraderRouter(registry)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine",
        "full_message_text": "OPEN $X — quick note no mention",
    })

    result = await router.run(ctx)
    assert result.status == "skip"
    assert "missing_alert_mention" in (result.reason or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_trader_router.py -v`
Expected: FAIL — modules don't exist.

- [ ] **Step 3: Create `agent/traders/registry.py`**

```python
from __future__ import annotations
from agent.traders.profile import TraderProfile


class TraderRegistry:
    def __init__(self, profiles: list[TraderProfile]) -> None:
        self._by_author: dict[str, TraderProfile] = {}
        self._bot_authors: dict[str, TraderProfile] = {}
        for p in profiles:
            self._by_author[p.discord_author_pattern] = p
            for bot in p.bot_authors_to_skip:
                self._bot_authors[bot] = p

    def lookup(self, author: str) -> TraderProfile | None:
        return self._by_author.get(author)

    def is_bot_author(self, author: str) -> TraderProfile | None:
        return self._bot_authors.get(author)

    def all(self) -> list[TraderProfile]:
        return list(self._by_author.values())
```

- [ ] **Step 4: Create `skills/signal/trader_router.py`**

```python
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.traders.registry import TraderRegistry


class TraderRouter(Skill):
    name = "TraderRouter"

    def __init__(self, registry: TraderRegistry) -> None:
        self._registry = registry

    async def run(self, ctx: Context) -> SkillResult:
        author = ctx.get("author", "")

        if self._registry.is_bot_author(author):
            return SkillResult(status="skip", reason=f"bot_author:{author}")

        profile = self._registry.lookup(author)
        if profile is None:
            return SkillResult(status="skip", reason=f"no_trader_profile:{author}")

        msg = ctx.get("full_message_text", "")
        if profile.require_alert_mention and profile.alert_mention not in msg:
            return SkillResult(
                status="skip",
                reason=f"missing_alert_mention:{profile.alert_mention}",
            )

        return SkillResult(
            status="success",
            updates={
                "trader_handle": profile.handle,
                "trader_auto_execute": profile.auto_execute,
            },
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_trader_router.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/traders/registry.py skills/signal/trader_router.py tests/unit/test_trader_router.py
git commit -m "feat(signal): TraderRouter skill resolves Discord author to profile"
```

---

## Task 6: `TraderClassifier` skill — fast-path + LLM classify

**Files:**
- Create: `skills/signal/trader_classifier.py`
- Test: `tests/unit/test_trader_classifier.py`

This skill does feature extraction, the deterministic shortcut, the prompt-cached LLM call, and confidence routing. It writes `bucket`, `confidence`, `size_pct`, `size_source`, `ticker`, `side`, plus the feature/llm payloads needed by the log writer downstream.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_trader_classifier.py`:

```python
import json
import pytest
from agent.context import Context
from agent.traders.profile import TraderProfile, ConvictionExample
from agent.traders.registry import TraderRegistry
from skills.signal.trader_classifier import TraderClassifier


def make_profile(handle="wse", auto=True, size_in_msg=True) -> TraderProfile:
    return TraderProfile(
        handle=handle, display_name="Wall St Engine",
        discord_author_pattern="Wall St Engine",
        alert_mention="@Wall - Alerts", require_alert_mention=True,
        bot_authors_to_skip=[], auto_execute=auto,
        size_in_message=size_in_msg, prefer_message_size=True,
        classifier_model="claude-haiku-4-5",
        availability_phrases=[],
        conviction_examples=[
            ConvictionExample(msg="Added 2% pos AUDC", bucket="LOW", why="2% small"),
            ConvictionExample(msg="upsizing core ENS aggressively", bucket="HIGH", why="upsize core"),
            ConvictionExample(msg="watching TEST closely", bucket="SKIP", why="no entry"),
        ],
    )


class FakeLLM:
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    async def classify(self, *, system: list, model: str, messages: list) -> dict:
        self.calls.append({"system": system, "model": model, "messages": messages})
        return self._response


@pytest.mark.asyncio
async def test_shortcut_path_uses_stated_size_no_llm_call():
    profile = make_profile()
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "AUDC", "side": "long",
                   "bucket": "LOW", "confidence": 0.5, "reason": "should not be used"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine",
        "trader_handle": "wse",
        "full_message_text": "Added 2% AUDC pos. on back of earnings",
    })

    result = await classifier.run(ctx)

    assert result.status == "success"
    assert ctx.get("size_pct") == 0.02
    assert ctx.get("size_source") == "shortcut_stated"
    assert ctx.get("ticker") == "AUDC"
    assert ctx.get("bucket") == "LOW"
    assert llm.calls == [], "shortcut path must not call LLM synchronously"


@pytest.mark.asyncio
async def test_llm_path_high_confidence_high_bucket_fires_at_10pct():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "AOSL", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "long idea thesis"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "Alpha + Omega Semiconductor long idea — deep thesis...",
    })

    result = await classifier.run(ctx)

    assert result.status == "success"
    assert ctx.get("bucket") == "HIGH"
    assert ctx.get("size_pct") == 0.10
    assert ctx.get("size_source") == "bucket_high"


@pytest.mark.asyncio
async def test_llm_path_mid_confidence_downgrades_to_low_5pct():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "X", "side": "long",
                   "bucket": "HIGH", "confidence": 0.65, "reason": "ambiguous"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "thinking about loading X here, looks interesting",
    })

    result = await classifier.run(ctx)
    assert ctx.get("bucket") == "LOW"
    assert ctx.get("size_pct") == 0.05
    assert ctx.get("size_source") == "downgrade"


@pytest.mark.asyncio
async def test_llm_path_low_confidence_drops():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "Z", "side": "long",
                   "bucket": "LOW", "confidence": 0.3, "reason": "very ambiguous"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "kind of interesting maybe",
    })

    result = await classifier.run(ctx)
    assert result.status == "skip"
    assert "low_confidence" in (result.reason or "")
    assert ctx.get("size_pct") == 0.0


@pytest.mark.asyncio
async def test_llm_skip_response_skips_pipeline():
    profile = make_profile(size_in_msg=False)
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": False, "ticker": None, "side": None,
                   "bucket": "SKIP", "confidence": 0.9, "reason": "commentary"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "great results for $OSS — Revenue +70% Y/Y",
    })

    result = await classifier.run(ctx)
    assert result.status == "skip"
    assert ctx.get("bucket") == "SKIP"


@pytest.mark.asyncio
async def test_stated_size_capped_at_10pct():
    profile = make_profile()
    registry = TraderRegistry([profile])
    llm = FakeLLM({"is_entry": True, "ticker": "X", "side": "long",
                   "bucket": "LOW", "confidence": 0.9, "reason": "x"})
    classifier = TraderClassifier(registry, llm)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "Wall St Engine", "trader_handle": "wse",
        "full_message_text": "Added 20% pos in X",
    })

    result = await classifier.run(ctx)
    assert ctx.get("size_pct") == 0.10
    assert ctx.get("size_source") == "shortcut_stated"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_trader_classifier.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `skills/signal/trader_classifier.py`**

```python
from __future__ import annotations
import json
import logging
from typing import Protocol

from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.traders.registry import TraderRegistry
from skills.signal.feature_extractor import extract_features


logger = logging.getLogger(__name__)

HIGH_CONF_THRESHOLD = 0.80
DROP_CONF_THRESHOLD = 0.50
SIZE_LOW = 0.05
SIZE_HIGH = 0.10
MAX_STATED_SIZE = 0.10  # cap at 10%

_SYSTEM_PREAMBLE = """You classify Discord trading messages from a single trader into one of three buckets: HIGH, LOW, SKIP.

Definitions:
- HIGH: clearly high-conviction entry (deep thesis, "core", "upsize", "high conviction", multi-paragraph reasoning).
- LOW: any actionable entry that is not HIGH — starters, swing trades, "small", "stab", explicit small percentages, standard adds.
- SKIP: commentary, news headlines, watchlist, exits, portfolio recaps, "no position", "fyi", macro takes, sympathy plays without entry.

Return JSON only:
{"is_entry": bool, "ticker": "SYMBOL"|null, "side": "long"|"short"|"none", "bucket": "HIGH"|"LOW"|"SKIP", "confidence": 0.0-1.0, "reason": "one sentence"}

Examples for THIS specific trader:
"""


def _build_system_prompt(profile) -> str:
    examples_block = "\n".join(
        f"- MSG: {e.msg!r}\n  BUCKET: {e.bucket}\n  WHY: {e.why}"
        for e in profile.conviction_examples
    )
    return _SYSTEM_PREAMBLE + examples_block


class LLMClassifierClient(Protocol):
    async def classify(self, *, system: list, model: str, messages: list) -> dict: ...


class TraderClassifier(Skill):
    name = "TraderClassifier"

    def __init__(self, registry: TraderRegistry, llm: LLMClassifierClient) -> None:
        self._registry = registry
        self._llm = llm

    async def run(self, ctx: Context) -> SkillResult:
        handle = ctx.get("trader_handle")
        profile = next((p for p in self._registry.all() if p.handle == handle), None)
        if profile is None:
            return SkillResult(status="fail", reason=f"trader_profile_not_found:{handle}")

        msg = ctx.get("full_message_text", "")
        features = extract_features(
            msg,
            availability_phrases=profile.availability_phrases,
        )

        # Deterministic shortcut: stated size + entry verb + exactly one ticker.
        if (
            profile.prefer_message_size
            and features.stated_size_pct is not None
            and features.entry_verb_present
            and len(features.tickers_in_msg) == 1
        ):
            size_pct = min(features.stated_size_pct / 100.0, MAX_STATED_SIZE)
            bucket = "HIGH" if size_pct >= SIZE_LOW * 1.5 else "LOW"  # >=7.5% → HIGH bookkeeping
            updates = {
                "ticker": features.tickers_in_msg[0],
                "side": "long",
                "bucket": bucket,
                "confidence": 1.0,
                "size_pct": size_pct,
                "size_source": "shortcut_stated",
                "classifier_features_json": json.dumps(features.__dict__),
                "classifier_llm_response_json": None,
                "classifier_reason": "stated_size_in_message",
            }
            ctx.update(updates)
            return SkillResult(status="success", updates=updates)

        # LLM path.
        system_prompt = _build_system_prompt(profile)
        try:
            response = await self._llm.classify(
                system=[{"type": "text", "text": system_prompt,
                         "cache_control": {"type": "ephemeral"}}],
                model=profile.classifier_model,
                messages=[{"role": "user", "content": msg}],
            )
        except Exception as exc:
            logger.exception("trader_classifier llm error: %s", exc)
            return SkillResult(status="fail", reason=f"llm_error:{exc}")

        bucket = response.get("bucket", "SKIP")
        confidence = float(response.get("confidence", 0.0))
        ticker = response.get("ticker")
        side = response.get("side", "none")
        reason = response.get("reason", "")

        features_json = json.dumps(features.__dict__)
        llm_json = json.dumps(response)

        if bucket == "SKIP" or not response.get("is_entry"):
            updates = {
                "bucket": "SKIP", "confidence": confidence,
                "size_pct": 0.0, "size_source": "skip",
                "classifier_features_json": features_json,
                "classifier_llm_response_json": llm_json,
                "classifier_reason": reason,
            }
            ctx.update(updates)
            return SkillResult(status="skip", updates=updates,
                               reason=f"classifier_skip:{reason}")

        if confidence < DROP_CONF_THRESHOLD:
            updates = {
                "bucket": bucket, "confidence": confidence,
                "size_pct": 0.0, "size_source": "drop_low_conf",
                "classifier_features_json": features_json,
                "classifier_llm_response_json": llm_json,
                "classifier_reason": reason,
            }
            ctx.update(updates)
            return SkillResult(status="skip", updates=updates,
                               reason=f"low_confidence:{confidence:.2f}")

        if confidence < HIGH_CONF_THRESHOLD:
            final_bucket = "LOW"
            size_pct = SIZE_LOW
            size_source = "downgrade"
        else:
            final_bucket = bucket
            size_pct = SIZE_HIGH if bucket == "HIGH" else SIZE_LOW
            size_source = "bucket_high" if bucket == "HIGH" else "bucket_low"

        updates = {
            "ticker": ticker, "side": side,
            "bucket": final_bucket, "confidence": confidence,
            "size_pct": size_pct, "size_source": size_source,
            "classifier_features_json": features_json,
            "classifier_llm_response_json": llm_json,
            "classifier_reason": reason,
        }
        ctx.update(updates)
        return SkillResult(status="success", updates=updates)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_trader_classifier.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/signal/trader_classifier.py tests/unit/test_trader_classifier.py
git commit -m "feat(signal): TraderClassifier with shortcut path and prompt-cached LLM call"
```

---

## Task 7: Anthropic LLM client adapter

**Files:**
- Create: `infra/llm/__init__.py`
- Create: `infra/llm/classifier_client.py`
- Test: `tests/unit/test_classifier_client.py`

The classifier needs an adapter that satisfies `LLMClassifierClient` and parses the JSON response from Anthropic.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_classifier_client.py`:

```python
import pytest
from infra.llm.classifier_client import AnthropicClassifierClient


class FakeContent:
    def __init__(self, text: str):
        self.text = text


class FakeResponse:
    def __init__(self, text: str):
        self.content = [FakeContent(text)]


class FakeAnthropic:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict] = []
        self.messages = self  # mimic SDK's nested attribute

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._text)


@pytest.mark.asyncio
async def test_classifier_parses_clean_json():
    fake = FakeAnthropic('{"is_entry": true, "ticker": "X", "side": "long", '
                        '"bucket": "LOW", "confidence": 0.85, "reason": "explicit"}')
    client = AnthropicClassifierClient(fake)
    out = await client.classify(
        system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert out["bucket"] == "LOW"
    assert out["confidence"] == 0.85


@pytest.mark.asyncio
async def test_classifier_extracts_json_from_wrapped_text():
    fake = FakeAnthropic('Here is the result:\n{"is_entry": false, "ticker": null, '
                        '"side": "none", "bucket": "SKIP", "confidence": 0.9, "reason": "x"}')
    client = AnthropicClassifierClient(fake)
    out = await client.classify(system=[], model="m", messages=[])
    assert out["bucket"] == "SKIP"


@pytest.mark.asyncio
async def test_classifier_raises_on_unparseable_response():
    fake = FakeAnthropic("totally not json here")
    client = AnthropicClassifierClient(fake)
    with pytest.raises(ValueError, match="parse"):
        await client.classify(system=[], model="m", messages=[])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_classifier_client.py -v`
Expected: FAIL.

- [ ] **Step 3: Create `infra/llm/__init__.py`** (empty file).

- [ ] **Step 4: Create `infra/llm/classifier_client.py`**

```python
from __future__ import annotations
import json
import re


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class AnthropicClassifierClient:
    def __init__(self, anthropic_client, max_tokens: int = 256) -> None:
        self._anth = anthropic_client
        self._max_tokens = max_tokens

    async def classify(self, *, system: list, model: str, messages: list) -> dict:
        response = await self._anth.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
        )
        text = response.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = _JSON_OBJECT.search(text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        raise ValueError(f"classifier_response_parse_error: {text[:200]}")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_classifier_client.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add infra/llm/ tests/unit/test_classifier_client.py
git commit -m "feat(llm): AnthropicClassifierClient adapter with JSON parsing"
```

---

## Task 8: `ClassificationLogStore`

**Files:**
- Create: `infra/storage/classification_log_store.py`
- Test: `tests/integration/test_classification_log_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_classification_log_store.py`:

```python
import pytest
import json
from infra.storage.classification_log_store import ClassificationLogStore


@pytest.mark.asyncio
async def test_insert_and_read_back(db):
    store = ClassificationLogStore(db)
    await store.insert(
        event_id="evt1", trader_handle="wse",
        msg_text="Added 2% AUDC", features={"x": 1},
        llm_response=None, bucket="LOW", confidence=1.0,
        size_pct=0.02, size_source="shortcut_stated",
        action_taken="fired", reason="stated_size_in_message",
    )
    rows = await store.recent_for_trader("wse", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "evt1"
    assert row["bucket"] == "LOW"
    assert row["size_pct"] == 0.02
    assert json.loads(row["features_json"]) == {"x": 1}
    assert row["llm_response_json"] is None


@pytest.mark.asyncio
async def test_recent_returns_newest_first(db):
    store = ClassificationLogStore(db)
    for i in range(3):
        await store.insert(
            event_id=f"e{i}", trader_handle="wse", msg_text=f"m{i}",
            features={}, llm_response=None, bucket="SKIP",
            confidence=0.9, size_pct=0.0, size_source="skip",
            action_taken="skipped", reason="x",
        )
    rows = await store.recent_for_trader("wse", limit=2)
    assert [r["event_id"] for r in rows] == ["e2", "e1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_classification_log_store.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `infra/storage/classification_log_store.py`**

```python
from __future__ import annotations
import json
from datetime import datetime, timezone


class ClassificationLogStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def insert(self, *, event_id: str, trader_handle: str, msg_text: str,
                     features: dict, llm_response: dict | None, bucket: str,
                     confidence: float, size_pct: float, size_source: str,
                     action_taken: str, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO classification_log
               (event_id, trader_handle, msg_text, features_json, llm_response_json,
                bucket, confidence, size_pct, size_source, action_taken, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, trader_handle, msg_text, json.dumps(features),
             json.dumps(llm_response) if llm_response is not None else None,
             bucket, confidence, size_pct, size_source, action_taken, reason, now),
        )
        await self._conn.commit()

    async def recent_for_trader(self, trader_handle: str, *, limit: int = 100) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM classification_log
               WHERE trader_handle = ?
               ORDER BY id DESC LIMIT ?""",
            (trader_handle, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_classification_log_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/storage/classification_log_store.py tests/integration/test_classification_log_store.py
git commit -m "feat(storage): ClassificationLogStore with recent_for_trader query"
```

---

## Task 9: `ExamplesPendingStore` and `TraderStateStore`

**Files:**
- Create: `infra/storage/examples_pending_store.py`
- Create: `infra/storage/trader_state_store.py`
- Test: `tests/integration/test_examples_pending_store.py`
- Test: `tests/integration/test_trader_state_store.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_examples_pending_store.py`:

```python
import pytest
from infra.storage.examples_pending_store import ExamplesPendingStore


@pytest.mark.asyncio
async def test_insert_and_list_pending(db):
    store = ExamplesPendingStore(db)
    await store.insert(trader_handle="wse", msg_text="x", proposed_bucket="LOW",
                       proposed_why="why", source="low_confidence")
    rows = await store.list_pending(trader_handle="wse")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_marks_status_and_bucket(db):
    store = ExamplesPendingStore(db)
    await store.insert(trader_handle="wse", msg_text="x", proposed_bucket="LOW",
                       proposed_why="w", source="low_confidence")
    pending = await store.list_pending(trader_handle="wse")
    pid = pending[0]["id"]
    await store.resolve(pid, status="approved", resolved_bucket="HIGH")
    remaining = await store.list_pending(trader_handle="wse")
    assert remaining == []
    resolved = await store.list_resolved(trader_handle="wse")
    assert resolved[0]["resolved_bucket"] == "HIGH"
    assert resolved[0]["status"] == "approved"
```

Create `tests/integration/test_trader_state_store.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from infra.storage.trader_state_store import TraderStateStore


@pytest.mark.asyncio
async def test_set_and_get_unavailable_until(db):
    store = TraderStateStore(db)
    until = datetime.now(timezone.utc) + timedelta(days=7)
    await store.set_unavailable_until(handle="mystic", until=until)
    got = await store.get_unavailable_until("mystic")
    assert got is not None
    assert abs((got - until).total_seconds()) < 1


@pytest.mark.asyncio
async def test_get_returns_none_when_no_state(db):
    store = TraderStateStore(db)
    assert await store.get_unavailable_until("nobody") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_examples_pending_store.py tests/integration/test_trader_state_store.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `infra/storage/examples_pending_store.py`**

```python
from __future__ import annotations
from datetime import datetime, timezone


class ExamplesPendingStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def insert(self, *, trader_handle: str, msg_text: str,
                     proposed_bucket: str, proposed_why: str, source: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._conn.execute(
            """INSERT INTO trader_examples_pending
               (trader_handle, msg_text, proposed_bucket, proposed_why, source, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (trader_handle, msg_text, proposed_bucket, proposed_why, source, now),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def list_pending(self, *, trader_handle: str | None = None) -> list[dict]:
        if trader_handle:
            cursor = await self._conn.execute(
                "SELECT * FROM trader_examples_pending WHERE status='pending' AND trader_handle=? ORDER BY id",
                (trader_handle,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM trader_examples_pending WHERE status='pending' ORDER BY id"
            )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_resolved(self, *, trader_handle: str) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM trader_examples_pending WHERE status!='pending' AND trader_handle=? ORDER BY id",
            (trader_handle,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def resolve(self, pending_id: int, *, status: str, resolved_bucket: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """UPDATE trader_examples_pending
               SET status=?, resolved_bucket=?, resolved_at=?
               WHERE id=?""",
            (status, resolved_bucket, now, pending_id),
        )
        await self._conn.commit()
```

- [ ] **Step 4: Implement `infra/storage/trader_state_store.py`**

```python
from __future__ import annotations
from datetime import datetime, timezone


class TraderStateStore:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def get_unavailable_until(self, handle: str) -> datetime | None:
        cursor = await self._conn.execute(
            "SELECT unavailable_until FROM trader_state WHERE trader_handle = ?",
            (handle,),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])

    async def set_unavailable_until(self, *, handle: str, until: datetime) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO trader_state (trader_handle, unavailable_until, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(trader_handle) DO UPDATE SET
                 unavailable_until=excluded.unavailable_until,
                 updated_at=excluded.updated_at""",
            (handle, until.isoformat(), now),
        )
        await self._conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/integration/test_examples_pending_store.py tests/integration/test_trader_state_store.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add infra/storage/examples_pending_store.py infra/storage/trader_state_store.py \
        tests/integration/test_examples_pending_store.py tests/integration/test_trader_state_store.py
git commit -m "feat(storage): ExamplesPendingStore and TraderStateStore"
```

---

## Task 10: `OrderSizer` reads `size_pct` from context

**Files:**
- Modify: `skills/execution/order_sizer.py`
- Modify: `tests/unit/test_order_sizer.py`

- [ ] **Step 1: Write the failing test**

Edit `tests/unit/test_order_sizer.py` (replace existing or add as new). Read the file first; then add this test:

```python
import pytest
from agent.context import Context
from skills.execution.order_sizer import OrderSizer


class FakeAccount:
    def __init__(self, bp): self.buying_power = bp


class FakeGateway:
    def __init__(self, bp=100_000.0, quote=10.0):
        self._account = FakeAccount(bp)
        self._quote = quote
    async def get_account_summary(self): return self._account
    async def get_quote(self, ticker): return self._quote


@pytest.mark.asyncio
async def test_order_sizer_uses_size_pct_from_ctx_for_stock():
    sizer = OrderSizer(policy=None, gateway=FakeGateway(bp=100_000, quote=20.0))
    ctx = Context(trace_id="t", event_id="e", data={
        "instrument_type": "stock", "ticker": "X",
        "size_pct": 0.05,
    })
    result = await sizer.run(ctx)
    assert result.status == "success"
    # 5% of 100k = 5000; at $20 → 250 shares
    assert result.updates["quantity"] == 250
    assert "size_pct=0.05" in result.updates["sizing_reason"]


@pytest.mark.asyncio
async def test_order_sizer_fails_when_size_pct_missing_from_ctx():
    sizer = OrderSizer(policy=None, gateway=FakeGateway())
    ctx = Context(trace_id="t", event_id="e", data={"instrument_type": "stock", "ticker": "X"})
    result = await sizer.run(ctx)
    assert result.status == "fail"
    assert "size_pct" in (result.reason or "")
```

(Remove or update any existing `test_order_sizer.py` cases that pass `conviction_bucket` instead of `size_pct`. Old tests that depend on `policy.sizing_policy.high_conviction_pct` should be deleted now that sizing is ctx-driven.)

- [ ] **Step 2: Run test to verify the new test fails**

Run: `pytest tests/unit/test_order_sizer.py -v`
Expected: FAIL — sizer still reads `conviction_bucket` from policy.

- [ ] **Step 3: Modify `skills/execution/order_sizer.py`**

Replace the body of `OrderSizer.run`:

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
        size_pct = ctx.get("size_pct")
        if size_pct is None or size_pct <= 0:
            return SkillResult(status="fail", reason="order_sizer: size_pct missing or <= 0 in context")

        try:
            account = await self._gateway.get_account_summary()
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        instrument_type = ctx.get("instrument_type", "option")
        allocation = account.buying_power * size_pct

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

        reason = f"size_pct={size_pct:.2f} of ${account.buying_power:,.0f} buying_power"
        logger.info("OrderSizer: qty=%d notional=%.2f (%s)", quantity, notional, reason)
        return SkillResult(status="success", updates={
            "quantity": quantity,
            "notional_estimate": notional,
            "sizing_reason": reason,
            "capped_by": None,
        })
```

- [ ] **Step 4: Run all order_sizer tests**

Run: `pytest tests/unit/test_order_sizer.py -v`
Expected: new tests PASS; if old tests still reference `conviction_bucket`, delete those cases.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/order_sizer.py tests/unit/test_order_sizer.py
git commit -m "refactor(order_sizer): read size_pct from context, drop policy.sizing_policy dependency"
```

---

## Task 11: Bootstrap-mode Telegram digest

**Files:**
- Modify: `skills/posttrade/telegram_digest.py`
- Test: `tests/integration/test_telegram_digest.py` (extend)

The `TraderClassifier` runs identically for autonomous and bootstrap traders. After classification, the orchestrator branches: if `trader_auto_execute` is False AND the message is not a SKIP, post to Telegram and stop the pipeline. We add a method on `TelegramDigest` for the bootstrap message.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_telegram_digest.py`:

```python
import pytest
from agent.context import Context
from skills.posttrade.telegram_digest import TelegramDigest


@pytest.mark.asyncio
async def test_bootstrap_review_digest_includes_classification_details(telegram):
    digest = TelegramDigest(telegram, mode="signal_only")
    ctx = Context(trace_id="t1", event_id="e1", data={
        "trader_handle": "mystic",
        "author": "UndefinedMystic",
        "channel": "alerts",
        "ticker": "INDI",
        "bucket": "LOW",
        "confidence": 0.72,
        "size_pct": 0.05,
        "classifier_reason": "small + swing trade self-label",
        "full_message_text": "i opened a small swing trade in INDI",
    })
    await digest.send_bootstrap_review_digest(ctx)
    assert len(telegram.sent) == 1
    body = telegram.sent[0]
    assert "BOOTSTRAP REVIEW" in body
    assert "mystic" in body
    assert "INDI" in body
    assert "LOW" in body
    assert "5%" in body
    assert "0.72" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_telegram_digest.py -v -k bootstrap`
Expected: FAIL — method not implemented.

- [ ] **Step 3: Implement `send_bootstrap_review_digest` on `TelegramDigest`**

Add this method to `skills/posttrade/telegram_digest.py`:

```python
    async def send_bootstrap_review_digest(self, ctx: Context) -> None:
        import html
        trader = html.escape(ctx.get("trader_handle", "?"))
        author = html.escape(ctx.get("author", "?"))
        channel = html.escape(ctx.get("channel", "?"))
        ticker = html.escape(ctx.get("ticker") or "(no-ticker)")
        bucket = html.escape(ctx.get("bucket", "?"))
        size_pct = ctx.get("size_pct", 0.0)
        size_display = f"{size_pct * 100:.0f}%"
        confidence = ctx.get("confidence", 0.0)
        why = html.escape(ctx.get("classifier_reason", ""))
        msg = html.escape(ctx.get("full_message_text", ""))
        text = (
            f"<b>BOOTSTRAP REVIEW</b>\n\n"
            f"Trader: {trader} ({author})\n"
            f"Channel: #{channel}\n"
            f"Ticker: <b>{ticker}</b>\n"
            f"Proposed: <b>{bucket}</b> @ {size_display} (conf {confidence:.2f})\n"
            f"Why: <i>{why}</i>\n\n"
            f"Message:\n<i>{msg}</i>\n\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Bootstrap review delivery failed: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_telegram_digest.py -v -k bootstrap`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/posttrade/telegram_digest.py tests/integration/test_telegram_digest.py
git commit -m "feat(telegram): bootstrap review digest format for non-autonomous traders"
```

---

## Task 12: Wire pipeline in `agent/registry.py`

**Files:**
- Modify: `agent/registry.py`
- Test: `tests/integration/test_pipeline_phase1_traders.py`

The new Phase 1 chain: `MessageNormalizer → DesktopReader → TraderRouter → TraderClassifier → IdempotencyCheck → TickerValidator → ClassificationLogger → BootstrapReviewGate → TelegramDigest(if applicable)`.

We introduce two thin internal skills here for plumbing — `ClassificationLogger` writes to `classification_log`; `BootstrapReviewGate` short-circuits the pipeline (returning `skip`) if the trader is in bootstrap mode and the message is an actionable entry, after first dispatching the bootstrap digest.

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_pipeline_phase1_traders.py`:

```python
import pytest
from pathlib import Path
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.traders.profile import load_all_profiles
from agent.traders.registry import TraderRegistry
from skills.signal.trader_router import TraderRouter
from skills.signal.trader_classifier import TraderClassifier
from infra.storage.classification_log_store import ClassificationLogStore
from infra.storage.trace_store import TraceStore


REPO_ROOT = Path(__file__).resolve().parents[2]


class StubLLM:
    def __init__(self, response): self._r = response
    async def classify(self, **kw): return self._r


class CapturingTelegram:
    def __init__(self): self.sent = []
    async def send_message(self, text): self.sent.append(text)


@pytest.mark.asyncio
async def test_wse_shortcut_path_logs_classification_no_llm_call(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)

    llm = StubLLM({"is_entry": False, "ticker": None, "side": "none",
                   "bucket": "SKIP", "confidence": 0.99, "reason": "noop"})

    from skills.signal.classification_logger import ClassificationLogger
    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
    ]

    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t1", event_id="e1", data={
        "author": "Wall St Engine",
        "channel": "alerts",
        "full_message_text": "Added 2% AUDC pos. @Wall - Alerts",
    })
    await orch.run(ctx)

    rows = await log_store.recent_for_trader("wallstengine")
    assert len(rows) == 1
    assert rows[0]["size_source"] == "shortcut_stated"
    assert rows[0]["llm_response_json"] is None


@pytest.mark.asyncio
async def test_mystic_bootstrap_mode_posts_to_telegram_and_skips(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)
    telegram_client = CapturingTelegram()

    llm = StubLLM({"is_entry": True, "ticker": "INDI", "side": "long",
                   "bucket": "LOW", "confidence": 0.85,
                   "reason": "small swing trade self-label"})

    from skills.signal.classification_logger import ClassificationLogger
    from skills.signal.bootstrap_review_gate import BootstrapReviewGate
    from skills.posttrade.telegram_digest import TelegramDigest

    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
        BootstrapReviewGate(TelegramDigest(telegram_client)),
    ]
    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t2", event_id="e2", data={
        "author": "UndefinedMystic",
        "channel": "alerts",
        "full_message_text": "i opened a small swing trade position in INDI @Alerts - Mystic",
    })
    await orch.run(ctx)

    assert any("BOOTSTRAP REVIEW" in m for m in telegram_client.sent)
    rows = await log_store.recent_for_trader("mystic")
    assert rows[0]["action_taken"] == "bootstrap_review"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_pipeline_phase1_traders.py -v`
Expected: FAIL — `classification_logger` and `bootstrap_review_gate` don't exist.

- [ ] **Step 3: Create `skills/signal/classification_logger.py`**

```python
from __future__ import annotations
import json
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.storage.classification_log_store import ClassificationLogStore


logger = logging.getLogger(__name__)


class ClassificationLogger(Skill):
    name = "ClassificationLogger"

    def __init__(self, store: ClassificationLogStore) -> None:
        self._store = store

    async def run(self, ctx: Context) -> SkillResult:
        bucket = ctx.get("bucket")
        if bucket is None:
            return SkillResult(status="success")  # nothing classified — earlier skip

        size_pct = ctx.get("size_pct", 0.0)
        size_source = ctx.get("size_source", "skip")
        confidence = float(ctx.get("confidence", 0.0))
        action_taken = self._infer_action(ctx, bucket)

        features_json = ctx.get("classifier_features_json", "{}")
        llm_json = ctx.get("classifier_llm_response_json")

        try:
            await self._store.insert(
                event_id=ctx.event_id,
                trader_handle=ctx.get("trader_handle", "unknown"),
                msg_text=ctx.get("full_message_text", ""),
                features=json.loads(features_json) if features_json else {},
                llm_response=json.loads(llm_json) if llm_json else None,
                bucket=bucket, confidence=confidence,
                size_pct=size_pct, size_source=size_source,
                action_taken=action_taken,
                reason=ctx.get("classifier_reason", ""),
            )
        except Exception as exc:
            logger.exception("classification_logger failed: %s", exc)
        return SkillResult(status="success")

    @staticmethod
    def _infer_action(ctx: Context, bucket: str) -> str:
        if bucket == "SKIP":
            return "skipped"
        if not ctx.get("trader_auto_execute", False):
            return "bootstrap_review"
        return "fired"
```

- [ ] **Step 4: Create `skills/signal/bootstrap_review_gate.py`**

```python
from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill


class BootstrapReviewGate(Skill):
    """When trader is non-autonomous and the message is actionable, post a
    review digest and stop the pipeline (status=skip)."""

    name = "BootstrapReviewGate"

    def __init__(self, telegram_digest) -> None:
        self._digest = telegram_digest

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("trader_auto_execute", True):
            return SkillResult(status="success")
        if ctx.get("bucket") in (None, "SKIP"):
            return SkillResult(status="success")
        await self._digest.send_bootstrap_review_digest(ctx)
        return SkillResult(status="skip", reason="bootstrap_review_posted")
```

- [ ] **Step 5: Update `agent/registry.py` Phase 1 builder**

Replace `build_phase1_chain` with a version that uses the new skills. Keep the old name and signature so call sites don't break, but require the registry/store args:

```python
from agent.skill import Skill


def build_phase1_chain(policy, idempotency_store, telegram_client, gateway=None,
                       trader_registry=None, classification_log_store=None,
                       llm_classifier=None) -> list:
    from skills.signal.message_normalizer import MessageNormalizer
    from skills.signal.desktop_reader import DesktopReader
    from skills.signal.trader_router import TraderRouter
    from skills.signal.trader_classifier import TraderClassifier
    from skills.signal.classification_logger import ClassificationLogger
    from skills.signal.bootstrap_review_gate import BootstrapReviewGate
    from skills.risk.idempotency_check import IdempotencyCheck
    from skills.posttrade.telegram_digest import TelegramDigest

    if trader_registry is None:
        raise ValueError("trader_registry is required for the conviction-classifier pipeline")
    if classification_log_store is None:
        raise ValueError("classification_log_store is required")
    if llm_classifier is None:
        raise ValueError("llm_classifier is required")

    digest = TelegramDigest(telegram_client, mode="signal_only")
    skills_list: list[Skill] = [
        MessageNormalizer(policy),
        DesktopReader(policy),
        TraderRouter(trader_registry),
        TraderClassifier(trader_registry, llm_classifier),
        ClassificationLogger(classification_log_store),
        BootstrapReviewGate(digest),
        IdempotencyCheck(policy, idempotency_store),
    ]
    if gateway is not None:
        from skills.signal.ticker_validator import TickerValidator
        skills_list.append(TickerValidator(gateway))

    skills_list.append(digest)
    return skills_list
```

(`build_phase2b_execution_chain` is unchanged — `OrderSizer` already reads `size_pct` from ctx after Task 10.)

- [ ] **Step 6: Run integration test**

Run: `pytest tests/integration/test_pipeline_phase1_traders.py -v`
Expected: PASS.

- [ ] **Step 7: Run new tests only — old e2e tests will break in Task 13**

Run: `pytest tests/integration/test_pipeline_phase1_traders.py tests/unit/test_trader_classifier.py tests/unit/test_trader_router.py tests/unit/test_feature_extractor.py -x -q`
Expected: all green. (Legacy `test_signal_analyzer.py`, `test_conviction_classifier.py`, and the e2e Phase 1 tests still reference the legacy skills and will be removed/updated in Task 13.)

- [ ] **Step 8: Commit**

```bash
git add skills/signal/classification_logger.py skills/signal/bootstrap_review_gate.py \
        agent/registry.py tests/integration/test_pipeline_phase1_traders.py
git commit -m "feat(pipeline): wire TraderRouter+Classifier+Logger+BootstrapGate into phase1"
```

---

## Task 13: Delete legacy skills and clean up downstream

The user has not run the legacy `SignalAnalyzer` / `ConvictionClassifier` pipeline in production. Deleting outright avoids dead-code maintenance and forces every reference to use the new `bucket` / `size_pct` ctx keys.

**Files deleted:**
- `skills/signal/signal_analyzer.py`
- `skills/signal/conviction_classifier.py`
- `tests/unit/test_signal_analyzer.py`
- `tests/unit/test_conviction_classifier.py`

**Files modified:**
- `agent/policy.py` — drop `SizingPolicy` class and field on `PolicyModel`
- `config/policy.yaml` — drop the `sizing_policy:` block
- `skills/posttrade/telegram_digest.py` — `_format_signal_digest` reads `bucket` and `size_pct` instead of `conviction_bucket` / `target_allocation_pct`
- `skills/execution/trade_intent_writer.py` — read `bucket` for the `conviction` column instead of `conviction_bucket`
- `tests/unit/test_policy.py` — remove all `sizing_policy` blocks and assertions
- `tests/unit/test_trade_intent_writer.py` — replace `conviction_bucket` with `bucket`
- `tests/integration/test_telegram_digest.py` — replace `conviction_bucket` / `target_allocation_pct` with `bucket` / `size_pct`
- `tests/e2e/test_phase1_pipeline.py` — rewrite to use the new pipeline (`TraderRouter` + `TraderClassifier` + stub LLM)
- `tests/e2e/test_phase2b_execution_pipeline.py` — replace `conviction_bucket` with `bucket` and inject `size_pct` directly into ctx

- [ ] **Step 1: Delete the legacy skill files and their unit tests**

```bash
git rm skills/signal/signal_analyzer.py skills/signal/conviction_classifier.py \
       tests/unit/test_signal_analyzer.py tests/unit/test_conviction_classifier.py
```

- [ ] **Step 2: Strip `SizingPolicy` from `agent/policy.py`**

Open `agent/policy.py`. Delete the `SizingPolicy` class definition (lines around 24–26):

```python
class SizingPolicy(BaseModel):
    low_conviction_pct: float
    high_conviction_pct: float
```

In `PolicyModel`, delete the line `sizing_policy: SizingPolicy`.

- [ ] **Step 3: Strip `sizing_policy:` from `config/policy.yaml`**

Edit `config/policy.yaml`. Delete the block:

```yaml
sizing_policy:
  low_conviction_pct: 0.05
  high_conviction_pct: 0.10
```

- [ ] **Step 4: Update `skills/posttrade/telegram_digest.py`**

In `_format_signal_digest`, replace:

```python
allocation_pct = ctx.get("target_allocation_pct", 0)
pct_display = f"{allocation_pct * 100:.0f}%"
...
conviction = html.escape(ctx.get("conviction_bucket", "?"))
...
f"Conviction: {conviction} → {pct_display} allocation\n\n"
```

with:

```python
size_pct = ctx.get("size_pct", 0.0)
pct_display = f"{size_pct * 100:.0f}%"
...
bucket = html.escape(ctx.get("bucket", "?"))
...
f"Bucket: {bucket} → {pct_display} allocation\n\n"
```

- [ ] **Step 5: Update `skills/execution/trade_intent_writer.py`**

Replace line 34:

```python
conviction = ctx.get("conviction") or ctx.get("conviction_bucket", "medium")
```

with:

```python
conviction = ctx.get("bucket") or ctx.get("conviction", "LOW")
```

(The DB column stays `conviction`; we're just sourcing its value from the new `bucket` key.)

- [ ] **Step 6: Update `tests/unit/test_policy.py`**

Remove all `sizing_policy:` blocks and the matching assertions (lines around 19–21, 51, 76–78, 132–134, 185–187). Run the test to confirm it still passes the trimmed schema.

- [ ] **Step 7: Update `tests/unit/test_trade_intent_writer.py`**

In every test that sets `conviction_bucket=...` in ctx, rename to `bucket=...`. Update the helper around line 14–21 accordingly. The DB record still has a `conviction` column.

- [ ] **Step 8: Update `tests/integration/test_telegram_digest.py`**

Replace every `"conviction_bucket": "..."` with `"bucket": "..."`, and `"target_allocation_pct": 0.10` with `"size_pct": 0.10`. Update assertions that scan for "Conviction:" to look for "Bucket:" instead.

- [ ] **Step 9: Rewrite `tests/e2e/test_phase1_pipeline.py`**

Read the file, replace the chain construction. Skeleton:

```python
import json
import pytest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from agent.context import Context
from agent.orchestrator import Orchestrator
from agent.traders.profile import load_all_profiles
from agent.traders.registry import TraderRegistry
from skills.signal.trader_router import TraderRouter
from skills.signal.trader_classifier import TraderClassifier
from skills.signal.classification_logger import ClassificationLogger
from infra.storage.classification_log_store import ClassificationLogStore
from infra.storage.trace_store import TraceStore


REPO_ROOT = Path(__file__).resolve().parents[2]


class StubLLM:
    def __init__(self, response: dict): self._r = response
    async def classify(self, **kw): return self._r


@pytest.mark.asyncio
async def test_phase1_high_bucket_signal(db):
    profiles = load_all_profiles(REPO_ROOT / "config" / "traders")
    registry = TraderRegistry(profiles)
    log_store = ClassificationLogStore(db)
    trace_store = TraceStore(db)

    llm = StubLLM({"is_entry": True, "ticker": "AOSL", "side": "long",
                   "bucket": "HIGH", "confidence": 0.9, "reason": "long idea"})
    skills = [
        TraderRouter(registry),
        TraderClassifier(registry, llm),
        ClassificationLogger(log_store),
    ]
    orch = Orchestrator(skills, trace_store)
    ctx = Context(trace_id="t", event_id="e", data={
        "author": "UndefinedMystic",
        "channel": "alerts",
        "full_message_text": "Alpha + Omega Semiconductor long idea — deep thesis @Alerts - Mystic",
    })
    await orch.run(ctx)

    rows = await log_store.recent_for_trader("mystic")
    assert rows[0]["bucket"] == "HIGH"
    assert rows[0]["size_pct"] == 0.10
```

(Mirror prior structure: keep tests for skip path, low bucket path, etc. — but with new ctx keys.)

- [ ] **Step 10: Update `tests/e2e/test_phase2b_execution_pipeline.py`**

In every test, replace `p.sizing_policy.low_conviction_pct = 0.05` / `p.sizing_policy.high_conviction_pct = 0.10` with nothing (those fields are gone). In ctx setup, replace `"conviction_bucket": "high"` with `"size_pct": 0.10`. Remove `low_pct` / `high_pct` arguments from the policy fixture.

- [ ] **Step 11: Run the full test suite**

Run: `pytest tests -x -q`
Expected: all green. Any remaining `conviction_bucket` / `target_allocation_pct` / `sizing_policy` references should already be gone.

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "refactor: delete legacy SignalAnalyzer/ConvictionClassifier and migrate tests to bucket/size_pct"
```

---

## Task 14: `bin/promote_examples.py` — CLI to approve pending examples

**Files:**
- Create: `bin/promote_examples.py`
- Test: `tests/integration/test_promote_examples.py`

The CLI lists pending entries, prompts the user (or accepts a non-interactive flag) to approve / set the bucket, marks them resolved, and appends the approved ones to the trader's YAML profile in-place.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_promote_examples.py`:

```python
import pytest
import yaml
from pathlib import Path
from infra.storage.examples_pending_store import ExamplesPendingStore
from bin.promote_examples import promote_one


@pytest.mark.asyncio
async def test_promote_appends_to_yaml_and_resolves_pending(db, tmp_path: Path):
    yaml_path = tmp_path / "wse.yaml"
    yaml_path.write_text(
        "handle: wallstengine\n"
        "display_name: Wall St Engine\n"
        "discord_author_pattern: \"Wall St Engine\"\n"
        "alert_mention: \"@Wall - Alerts\"\n"
        "require_alert_mention: true\n"
        "bot_authors_to_skip: []\n"
        "auto_execute: true\n"
        "size_in_message: true\n"
        "prefer_message_size: true\n"
        "classifier_model: claude-haiku-4-5\n"
        "availability_phrases: []\n"
        "conviction_examples:\n"
        "  - msg: existing\n"
        "    bucket: LOW\n"
        "    why: seed\n"
    )

    store = ExamplesPendingStore(db)
    pending_id = await store.insert(
        trader_handle="wallstengine", msg_text="brand new phrasing here",
        proposed_bucket="LOW", proposed_why="ambiguous low conf",
        source="low_confidence",
    )

    await promote_one(store, pending_id, yaml_path, approved_bucket="HIGH",
                      why_override="manual upgrade")

    data = yaml.safe_load(yaml_path.read_text())
    examples = data["conviction_examples"]
    assert len(examples) == 2
    assert examples[1] == {"msg": "brand new phrasing here", "bucket": "HIGH",
                           "why": "manual upgrade"}

    remaining = await store.list_pending(trader_handle="wallstengine")
    assert remaining == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_promote_examples.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `bin/promote_examples.py`**

```python
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path
import yaml
from infra.storage.db import get_connection
from infra.storage.examples_pending_store import ExamplesPendingStore


async def promote_one(store: ExamplesPendingStore, pending_id: int,
                      yaml_path: Path, *, approved_bucket: str,
                      why_override: str | None = None) -> None:
    pending = await _find_pending(store, pending_id)
    if pending is None:
        raise SystemExit(f"pending id {pending_id} not found or already resolved")

    raw = yaml.safe_load(yaml_path.read_text())
    raw.setdefault("conviction_examples", []).append({
        "msg": pending["msg_text"],
        "bucket": approved_bucket,
        "why": why_override or pending.get("proposed_why") or "",
    })
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    await store.resolve(pending_id, status="approved", resolved_bucket=approved_bucket)


async def _find_pending(store: ExamplesPendingStore, pending_id: int) -> dict | None:
    rows = await store.list_pending()
    for r in rows:
        if r["id"] == pending_id:
            return r
    return None


async def _list(store: ExamplesPendingStore, trader: str | None) -> None:
    rows = await store.list_pending(trader_handle=trader)
    for r in rows:
        print(f"[{r['id']}] {r['trader_handle']}  bucket={r['proposed_bucket']}  "
              f"src={r['source']}  why={r['proposed_why']!r}")
        print(f"     msg: {r['msg_text'][:120]!r}")


async def _async_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="agent.db")
    parser.add_argument("--traders-dir", default="config/traders")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--trader")

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("--id", type=int, required=True)
    p_approve.add_argument("--bucket", required=True, choices=["HIGH", "LOW", "SKIP"])
    p_approve.add_argument("--why")

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("--id", type=int, required=True)

    args = parser.parse_args(argv)
    conn = await get_connection(args.db)
    try:
        store = ExamplesPendingStore(conn)
        if args.cmd == "list":
            await _list(store, args.trader)
            return 0
        if args.cmd == "approve":
            pending = await _find_pending(store, args.id)
            if pending is None:
                print(f"pending id {args.id} not found", file=sys.stderr)
                return 2
            yaml_path = Path(args.traders_dir) / f"{pending['trader_handle']}.yaml"
            if not yaml_path.exists():
                print(f"profile yaml missing: {yaml_path}", file=sys.stderr)
                return 2
            await promote_one(store, args.id, yaml_path,
                              approved_bucket=args.bucket, why_override=args.why)
            print(f"promoted id={args.id} → {pending['trader_handle']} as {args.bucket}")
            return 0
        if args.cmd == "reject":
            await store.resolve(args.id, status="rejected", resolved_bucket=None)
            print(f"rejected id={args.id}")
            return 0
        return 1
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(_async_main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_promote_examples.py -v`
Expected: PASS.

- [ ] **Step 5: Smoke test the CLI manually**

Run: `python -m bin.promote_examples --db /tmp/_promote_test.db list`
Expected: no error, empty list (since DB is fresh).

- [ ] **Step 6: Commit**

```bash
git add bin/promote_examples.py tests/integration/test_promote_examples.py
git commit -m "feat(cli): bin/promote_examples for appending approved examples to YAML"
```

---

## Task 15: End-to-end smoke test through `main.py`

**Files:**
- Modify: `main.py` (read once; identify the place where Phase 1 chain is built and adapt to pass new dependencies)
- Test: manual + check `pytest tests/ -x -q`

This is the wire-up task. Open `main.py`, find where `build_phase1_chain` is called, and supply `trader_registry`, `classification_log_store`, `llm_classifier`. Construct them from the existing connection and policy.

- [ ] **Step 1: Read `main.py` to locate the call site**

Run: `grep -n "build_phase1_chain" main.py`

- [ ] **Step 2: Add construction of new dependencies**

Above the `build_phase1_chain(...)` call site, add:

```python
from agent.traders.profile import load_all_profiles
from agent.traders.registry import TraderRegistry
from infra.storage.classification_log_store import ClassificationLogStore
from infra.llm.classifier_client import AnthropicClassifierClient
import anthropic

trader_registry = TraderRegistry(load_all_profiles(Path("config/traders")))
classification_log_store = ClassificationLogStore(db_conn)
llm_classifier = AnthropicClassifierClient(anthropic.AsyncAnthropic())
```

(Adapt variable names to match what's already in `main.py`.)

- [ ] **Step 3: Pass the new arguments to `build_phase1_chain`**

```python
phase1 = build_phase1_chain(
    policy=policy,
    idempotency_store=idempotency_store,
    telegram_client=telegram_client,
    gateway=gateway,
    trader_registry=trader_registry,
    classification_log_store=classification_log_store,
    llm_classifier=llm_classifier,
)
```

- [ ] **Step 4: Run the full test suite**

Run: `pytest tests -x -q`
Expected: all green.

- [ ] **Step 5: Smoke-run the agent against a recorded sample event**

Run: `python -m agent.smoke_phase1` if such a script exists, else verify via `python -c "import main"` that imports succeed without error.

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat(main): wire trader_registry, classification log, llm classifier into phase1"
```

---

## Self-Review

**Spec coverage check:**

- ✅ Two-bucket sizing (5%/10%) → Task 6 (`SIZE_LOW`, `SIZE_HIGH` constants), Task 10 (`OrderSizer` reads ctx)
- ✅ Stated-size override capped at 10% → Task 6 (`MAX_STATED_SIZE` cap test)
- ✅ Per-trader YAML profiles → Tasks 2 + 3
- ✅ Single LLM call replacing SignalAnalyzer + ConvictionClassifier → Task 6
- ✅ Prompt-cached system prompt → Task 6 (`cache_control: ephemeral` in system list), Task 7 (passed through to anthropic SDK)
- ✅ Deterministic shortcut on stated size + entry verb → Task 6 (`test_shortcut_path_uses_stated_size_no_llm_call`)
- ✅ Confidence routing (≥0.80 / 0.50–0.80 downgrade / <0.50 drop) → Task 6
- ✅ classification_log writes for every classification → Tasks 1, 8, 12
- ✅ trader_examples_pending + trader_state schemas → Tasks 1, 9
- ✅ Bootstrap mode posts to Telegram and stops pipeline → Tasks 11, 12
- ✅ CLI to promote pending examples to YAML → Task 14
- ✅ Pipeline integration in `agent/registry.py` and `main.py` → Tasks 12, 15
- ✅ Legacy `SignalAnalyzer` / `ConvictionClassifier` deleted, `SizingPolicy` removed, downstream tests migrated → Task 13
- ⚠ **Out of scope (deferred per spec Non-Goals):** thread-aware confirmation, multi-ticker fan-out, P&L learning, reaction-listener for Telegram. The pending store exists; population by a reactions listener is not part of this plan. Manual entry via the CLI's `approve` subcommand serves as the v1 path.

**Placeholder scan:** No "TBD" / "TODO" / "implement later" / "similar to Task N". Each task contains complete code blocks for both tests and implementations.

**Type consistency:**
- `Features` dataclass referenced in Task 4 (defined) and Task 6 (used via `extract_features`). Match.
- `TraderProfile` defined in Task 2; used in Tasks 5, 6, 12. Field names consistent.
- `ConvictionExample` fields `msg`, `bucket`, `why` consistent across Tasks 2, 3, 13.
- `ctx["size_pct"]`, `ctx["size_source"]`, `ctx["bucket"]`, `ctx["confidence"]` set in Task 6, read in Tasks 8 (logger), 10 (OrderSizer), 11 (digest), 12 (gate). All consistent.
- `LLMClassifierClient` Protocol defined in Task 6; satisfied by `AnthropicClassifierClient` in Task 7 (`async classify(*, system, model, messages) -> dict`). Match.
- `ClassificationLogStore.insert` signature consistent across Tasks 8 and 12.
- `ExamplesPendingStore.insert` returns `int`; CLI in Task 14 uses returned id via `_find_pending` lookup, no signature mismatch.

All consistent.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-02-conviction-classification-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
