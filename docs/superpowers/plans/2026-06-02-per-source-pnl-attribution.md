# Per-Source P&L Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only report showing realized P&L per trader/channel (with per-ticker, equity-vs-options, and win-rate breakdowns), derived entirely from existing stores, surfaced as a CLI and an optional Telegram push.

**Architecture:** One pure aggregation function (`agent/pnl_attribution.py`, no I/O) is fed by a thin sync-`sqlite3` fetch layer in `bin/pnl_report.py`. The CLI prints a table by default and, with `--telegram`, also pushes a compact HTML summary via the existing `TelegramClient`. Realized P&L is per-lot: every sell is already keyed to an entry `intent_id`, so no FIFO matching is needed.

**Tech Stack:** Python 3, `sqlite3` (sync, read-only) for the CLI, `aiosqlite` stores for test seeding, `argparse`, `pytest` (asyncio_mode=auto), `httpx` (via existing `TelegramClient`).

**Spec:** `docs/superpowers/specs/2026-06-02-per-source-pnl-attribution-design.md`

---

## File Structure

- **Create** `agent/pnl_attribution.py` — pure accounting core: dataclasses + `compute_attribution(entries, trims, exits) -> AttributionReport`. No DB, no formatting, no I/O. The entire correctness surface.
- **Create** `bin/pnl_report.py` — CLI: sync `sqlite3` fetch (with `--channel` / `--since-entry` / `--since-sell` filtering), calls the pure core, renders a terminal table, optional `--telegram` HTML push.
- **Create** `tests/unit/test_pnl_attribution.py` — unit tests for the pure core.
- **Create** `tests/integration/test_pnl_report_cli.py` — end-to-end CLI test on a temp file DB seeded via the real stores.

### Row-shape contract (what the pure core reads, by key)
- entry (from `trade_intents`, only `execution_state='filled'`): `intent_id, channel, ticker, instrument_type, fill_price, fill_qty`.
- trim (from `trade_intent_trims`): `intent_id, sold_qty, sold_avg_price`.
- exit (from `position_exits`): `intent_id, sold_qty, sold_avg_price`.

Rows may be `sqlite3.Row`, `aiosqlite.Row`, or plain `dict` — the core accesses by `["key"]` only, so all three work.

---

## Task 1: Pure core — data model + empty report

**Files:**
- Create: `agent/pnl_attribution.py`
- Test: `tests/unit/test_pnl_attribution.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pnl_attribution.py
from agent.pnl_attribution import compute_attribution, AttributionReport


def test_empty_input_returns_empty_report():
    report = compute_attribution([], [], [])
    assert isinstance(report, AttributionReport)
    assert report.sources == []
    assert report.grand_total == 0.0
    assert report.total_closed_lots == 0
    assert report.total_wins == 0
    assert report.win_rate == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.pnl_attribution'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/pnl_attribution.py
"""Pure realized-P&L attribution. No DB, no I/O.

Realized P&L is per-lot: each filled entry intent is one lot, and every sell
(trim-ladder fire or follow-sell) is already keyed to that entry's intent_id,
so there is no FIFO/average-cost matching. See
docs/superpowers/specs/2026-06-02-per-source-pnl-attribution-design.md.

Row-shape contract (accessed by key; dict / sqlite3.Row / aiosqlite.Row all work):
  entry: intent_id, channel, ticker, instrument_type, fill_price, fill_qty
  trim:  intent_id, sold_qty, sold_avg_price
  exit:  intent_id, sold_qty, sold_avg_price
"""
from __future__ import annotations
from dataclasses import dataclass, field

OPTION_MULTIPLIER = 100


@dataclass
class TickerLine:
    ticker: str
    instrument_type: str  # 'equity' | 'option'
    realized: float
    closed_lots: int


@dataclass
class InstrumentBreakdown:
    equity: float = 0.0
    option: float = 0.0


@dataclass
class SourcePnl:
    channel: str
    realized: float = 0.0
    closed_lots: int = 0
    wins: int = 0
    losses: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_lot: float = 0.0
    worst_lot: float = 0.0
    by_instrument: InstrumentBreakdown = field(default_factory=InstrumentBreakdown)
    by_ticker: list[TickerLine] = field(default_factory=list)
    open_options: int = 0
    open_option_cost: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed_lots if self.closed_lots else 0.0


@dataclass
class AttributionReport:
    sources: list[SourcePnl] = field(default_factory=list)
    grand_total: float = 0.0
    total_closed_lots: int = 0
    total_wins: int = 0

    @property
    def win_rate(self) -> float:
        return self.total_wins / self.total_closed_lots if self.total_closed_lots else 0.0


def compute_attribution(entries, trims, exits) -> AttributionReport:
    return AttributionReport()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/pnl_attribution.py tests/unit/test_pnl_attribution.py
git commit -m "feat(pnl): pure attribution core scaffold + data model"
```

---

## Task 2: Realized per-lot math + per-source totals

Implements the realized formula `Σ(sold_qty × sold_avg_price) − (Σ sold_qty × fill_price)`, ×100 for options, summed per channel, with the equity/option split and grand total. Sells with `sold_qty<=0` or NULL price are skipped here (zero-fill records); their explicit flagging comes in Task 5.

**Files:**
- Modify: `agent/pnl_attribution.py`
- Test: `tests/unit/test_pnl_attribution.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_pnl_attribution.py

def _entry(intent_id, channel="stp", ticker="NVDA", itype="equity",
           fill_price=100.0, fill_qty=10):
    return {"intent_id": intent_id, "channel": channel, "ticker": ticker,
            "instrument_type": itype, "fill_price": fill_price, "fill_qty": fill_qty}


def _sell(intent_id, sold_qty=10, sold_avg_price=110.0):
    return {"intent_id": intent_id, "sold_qty": sold_qty,
            "sold_avg_price": sold_avg_price}


def test_full_close_gain():
    # bought 10 @ 100, sold 10 @ 110 -> +100
    report = compute_attribution(
        [_entry("a")], [_sell("a", 10, 110.0)], [])
    assert len(report.sources) == 1
    s = report.sources[0]
    assert s.channel == "stp"
    assert s.realized == 100.0
    assert s.by_instrument.equity == 100.0
    assert report.grand_total == 100.0


def test_full_close_loss():
    # bought 10 @ 100, sold 10 @ 90 -> -100
    report = compute_attribution([_entry("a")], [], [_sell("a", 10, 90.0)])
    assert report.sources[0].realized == -100.0


def test_partial_close_uses_sold_qty_not_fill_qty():
    # bought 10 @ 100, sold only 4 @ 110 -> +40 (not +100)
    report = compute_attribution([_entry("a")], [_sell("a", 4, 110.0)], [])
    assert report.sources[0].realized == 40.0


def test_trim_and_followsell_mix_on_one_lot():
    # bought 10 @ 100; trim 3 @ 110 (+30); follow-sell 5 @ 120 (+100) -> +130
    report = compute_attribution(
        [_entry("a")], [_sell("a", 3, 110.0)], [_sell("a", 5, 120.0)])
    assert report.sources[0].realized == 130.0


def test_option_lot_applies_100x_multiplier():
    # synthetic: option bought 1 @ 2.00, sold 1 @ 3.00 -> (3-2)*1*100 = +100
    e = _entry("opt", itype="option", fill_price=2.0, fill_qty=1)
    report = compute_attribution([e], [_sell("opt", 1, 3.0)], [])
    s = report.sources[0]
    assert s.realized == 100.0
    assert s.by_instrument.option == 100.0
    assert s.by_instrument.equity == 0.0


def test_multiple_sources_summed_and_sorted_desc():
    a = _entry("a", channel="stp")
    b = _entry("b", channel="mystic")
    report = compute_attribution(
        [a, b], [_sell("a", 10, 110.0), _sell("b", 10, 90.0)], [])
    assert [s.channel for s in report.sources] == ["stp", "mystic"]  # +100 before -100
    assert report.grand_total == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: FAIL — realized values are all `0.0` (stub returns empty report)

- [ ] **Step 3: Replace `compute_attribution` with the realized implementation**

```python
# replace the compute_attribution stub in agent/pnl_attribution.py
from collections import defaultdict


def _valid_sells(rows):
    """(sold_qty, sold_avg_price) for real sales only. Skips zero-fill
    (sold_qty<=0) and NULL-price rows; returns sell_anomaly=True if a row had
    sold_qty>0 but a NULL price (a real sale at an unknown price)."""
    out, sell_anomaly = [], False
    for r in rows:
        q, p = r["sold_qty"], r["sold_avg_price"]
        if q is None or q <= 0:
            continue
        if p is None:
            sell_anomaly = True
            continue
        out.append((int(q), float(p)))
    return out, sell_anomaly


def compute_attribution(entries, trims, exits) -> AttributionReport:
    trims_by = defaultdict(list)
    for t in trims:
        trims_by[t["intent_id"]].append(t)
    exits_by = defaultdict(list)
    for e in exits:
        exits_by[e["intent_id"]].append(e)

    by_channel: dict[str, SourcePnl] = {}

    def src(ch: str) -> SourcePnl:
        if ch not in by_channel:
            by_channel[ch] = SourcePnl(channel=ch)
        return by_channel[ch]

    for entry in entries:
        iid = entry["intent_id"]
        channel = entry["channel"] or "(unknown)"
        itype = entry["instrument_type"]
        fill_price = entry["fill_price"]
        sells, _sell_anomaly = _valid_sells(trims_by.get(iid, []) + exits_by.get(iid, []))

        sold_total = sum(q for q, _ in sells)
        proceeds = sum(q * p for q, p in sells)
        mult = OPTION_MULTIPLIER if itype == "option" else 1
        realized = (proceeds - sold_total * (fill_price or 0.0)) * mult

        s = src(channel)
        s.realized += realized
        if itype == "option":
            s.by_instrument.option += realized
        else:
            s.by_instrument.equity += realized

    sources = sorted(by_channel.values(), key=lambda s: s.realized, reverse=True)
    grand = sum(s.realized for s in sources)
    return AttributionReport(sources=sources, grand_total=grand)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: PASS (all Task 1 + Task 2 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/pnl_attribution.py tests/unit/test_pnl_attribution.py
git commit -m "feat(pnl): realized per-lot math + per-source totals"
```

---

## Task 3: Grouping — per-ticker lines, open options, report aggregates

Adds the per-(ticker, instrument_type) drill-down, the open-options count + cost basis, and the report-level `total_closed_lots`/`total_wins`. (Closed-lot counting itself lands here; win/loss stats land in Task 4.)

**Files:**
- Modify: `agent/pnl_attribution.py`
- Test: `tests/unit/test_pnl_attribution.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_pnl_attribution.py

def test_per_ticker_lines_grouped_by_ticker_and_instrument():
    nvda = _entry("a", channel="stp", ticker="NVDA")
    tsla = _entry("b", channel="stp", ticker="TSLA")
    report = compute_attribution(
        [nvda, tsla], [_sell("a", 10, 110.0), _sell("b", 10, 90.0)], [])
    s = report.sources[0]
    lines = {(l.ticker, l.instrument_type): l for l in s.by_ticker}
    assert lines[("NVDA", "equity")].realized == 100.0
    assert lines[("NVDA", "equity")].closed_lots == 1
    assert lines[("TSLA", "equity")].realized == -100.0


def test_open_option_lot_counted_and_costed_not_closed():
    # option with NO sells: open, $0 realized, cost basis = 2.0*1*100 = 200
    e = _entry("opt", channel="stp", ticker="AAPL", itype="option",
               fill_price=2.0, fill_qty=1)
    report = compute_attribution([e], [], [])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.open_options == 1
    assert s.open_option_cost == 200.0
    assert s.closed_lots == 0
    assert report.total_closed_lots == 0


def test_closed_lots_counted_at_report_level():
    a = _entry("a", channel="stp")
    b = _entry("b", channel="mystic")
    report = compute_attribution(
        [a, b], [_sell("a", 10, 110.0), _sell("b", 5, 90.0)], [])
    assert report.total_closed_lots == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: FAIL — `by_ticker` empty, `open_options`/`open_option_cost` zero, `total_closed_lots` zero

- [ ] **Step 3: Extend `compute_attribution`**

Replace the body of the `for entry in entries:` loop and the trailing return so the function reads as below (the head, `_valid_sells`, and dataclasses are unchanged):

```python
def compute_attribution(entries, trims, exits) -> AttributionReport:
    trims_by = defaultdict(list)
    for t in trims:
        trims_by[t["intent_id"]].append(t)
    exits_by = defaultdict(list)
    for e in exits:
        exits_by[e["intent_id"]].append(e)

    by_channel: dict[str, SourcePnl] = {}
    # (channel, ticker, instrument_type) -> [realized, closed_lots]
    ticker_acc: dict[tuple, list] = defaultdict(lambda: [0.0, 0])

    def src(ch: str) -> SourcePnl:
        if ch not in by_channel:
            by_channel[ch] = SourcePnl(channel=ch)
        return by_channel[ch]

    for entry in entries:
        iid = entry["intent_id"]
        channel = entry["channel"] or "(unknown)"
        ticker = entry["ticker"]
        itype = entry["instrument_type"]
        fill_price = entry["fill_price"]
        fill_qty = entry["fill_qty"] or 0
        sells, _sell_anomaly = _valid_sells(trims_by.get(iid, []) + exits_by.get(iid, []))

        sold_total = sum(q for q, _ in sells)
        proceeds = sum(q * p for q, p in sells)
        mult = OPTION_MULTIPLIER if itype == "option" else 1
        realized = (proceeds - sold_total * (fill_price or 0.0)) * mult
        is_closed = sold_total > 0

        s = src(channel)
        s.realized += realized
        if itype == "option":
            s.by_instrument.option += realized
        else:
            s.by_instrument.equity += realized

        key = (channel, ticker, itype)
        ticker_acc[key][0] += realized
        if is_closed:
            s.closed_lots += 1
            ticker_acc[key][1] += 1
        elif itype == "option":
            s.open_options += 1
            s.open_option_cost += (fill_price or 0.0) * fill_qty * OPTION_MULTIPLIER

    for (ch, ticker, itype), (rl, cl) in ticker_acc.items():
        by_channel[ch].by_ticker.append(TickerLine(ticker, itype, rl, cl))
    for s in by_channel.values():
        s.by_ticker.sort(key=lambda l: l.realized, reverse=True)

    sources = sorted(by_channel.values(), key=lambda s: s.realized, reverse=True)
    grand = sum(s.realized for s in sources)
    total_closed = sum(s.closed_lots for s in sources)
    return AttributionReport(sources=sources, grand_total=grand,
                             total_closed_lots=total_closed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/pnl_attribution.py tests/unit/test_pnl_attribution.py
git commit -m "feat(pnl): per-ticker grouping, open-options, closed-lot counts"
```

---

## Task 4: Stats — win rate, avg win/loss, best/worst

**Files:**
- Modify: `agent/pnl_attribution.py`
- Test: `tests/unit/test_pnl_attribution.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_pnl_attribution.py

def test_win_rate_and_avg_win_loss_and_extremes():
    # 3 closed lots in one source: +100, +40, -100
    a = _entry("a", ticker="NVDA")
    b = _entry("b", ticker="TSLA")
    c = _entry("c", ticker="AMD")
    report = compute_attribution(
        [a, b, c],
        [_sell("a", 10, 110.0), _sell("b", 4, 110.0), _sell("c", 10, 90.0)], [])
    s = report.sources[0]
    assert s.closed_lots == 3
    assert s.wins == 2
    assert s.losses == 1
    assert s.win_rate == 2 / 3
    assert s.avg_win == 70.0      # (100 + 40) / 2
    assert s.avg_loss == -100.0   # (-100) / 1
    assert s.best_lot == 100.0
    assert s.worst_lot == -100.0
    assert report.total_wins == 2
    assert report.win_rate == 2 / 3


def test_no_closed_lots_yields_zero_stats():
    e = _entry("opt", itype="option", fill_price=2.0, fill_qty=1)  # open option
    report = compute_attribution([e], [], [])
    s = report.sources[0]
    assert s.win_rate == 0.0
    assert s.avg_win == 0.0
    assert s.avg_loss == 0.0
    assert s.best_lot == 0.0
    assert s.worst_lot == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: FAIL — `wins`/`avg_win`/`best_lot` are zero (not yet computed)

- [ ] **Step 3: Add closed-lot stat tracking**

In `compute_attribution`, add a per-channel list of closed-lot realized values, append to it in the `if is_closed:` branch, and finalize stats. Concretely:

After the `def src(...)` helper, add:

```python
    closed_realized: dict[str, list] = defaultdict(list)
```

In the `if is_closed:` branch (inside the entry loop), after `s.closed_lots += 1`, add:

```python
            closed_realized[channel].append(realized)
            if realized > 0:
                s.wins += 1
            elif realized < 0:
                s.losses += 1
```

Before building `sources` (after the `by_ticker` sort loop), add the stats finalization:

```python
    for ch, s in by_channel.items():
        cr = closed_realized[ch]
        wins = [r for r in cr if r > 0]
        losses = [r for r in cr if r < 0]
        s.avg_win = sum(wins) / len(wins) if wins else 0.0
        s.avg_loss = sum(losses) / len(losses) if losses else 0.0
        s.best_lot = max(cr) if cr else 0.0
        s.worst_lot = min(cr) if cr else 0.0
```

Finally, include `total_wins` in the return:

```python
    total_wins = sum(s.wins for s in sources)
    return AttributionReport(sources=sources, grand_total=grand,
                             total_closed_lots=total_closed, total_wins=total_wins)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/pnl_attribution.py tests/unit/test_pnl_attribution.py
git commit -m "feat(pnl): per-source win rate, avg win/loss, best/worst"
```

---

## Task 5: Data-quality handling (zero-fill, NULL price, fill_price<=0)

Zero-fill exits are already skipped by `_valid_sells`. This task adds the two *flagged-and-excluded* anomalies: a real sell (`sold_qty>0`) with a NULL price, and an entry with `fill_price<=0`. Anomalous lots are kept out of all totals/stats and recorded in `source.flags`.

**Files:**
- Modify: `agent/pnl_attribution.py`
- Test: `tests/unit/test_pnl_attribution.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_pnl_attribution.py

def test_zero_fill_exit_is_ignored():
    # a zero-fill follow-sell (sold_qty=0, NULL price) must not close the lot
    report = compute_attribution(
        [_entry("a")], [],
        [{"intent_id": "a", "sold_qty": 0, "sold_avg_price": None}])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.closed_lots == 0
    assert s.flags == []  # zero-fill is normal, not an anomaly


def test_sell_with_null_price_is_flagged_and_excluded():
    # sold_qty>0 but price NULL: cannot value -> exclude, flag, lot not closed
    report = compute_attribution(
        [_entry("a", ticker="NVDA")], [],
        [{"intent_id": "a", "sold_qty": 5, "sold_avg_price": None}])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.closed_lots == 0
    assert any("NVDA" in f and "NULL price" in f for f in s.flags)


def test_zero_cost_entry_with_sells_is_flagged_and_excluded():
    # fill_price 0.0 would fabricate a phantom gain -> exclude + flag
    e = _entry("a", ticker="AMD", fill_price=0.0, fill_qty=10)
    report = compute_attribution([e], [_sell("a", 10, 50.0)], [])
    s = report.sources[0]
    assert s.realized == 0.0
    assert s.closed_lots == 0
    assert any("AMD" in f and "fill_price" in f for f in s.flags)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: FAIL — the NULL-price and zero-cost lots currently get counted/realized instead of flagged

- [ ] **Step 3: Add anomaly detection in the entry loop**

In `compute_attribution`, replace the block from `sells, _sell_anomaly = ...` down to (but not including) `s = src(channel)` with:

```python
        sells, sell_anomaly = _valid_sells(trims_by.get(iid, []) + exits_by.get(iid, []))

        s = src(channel)
        anomalous = False
        if sells and (fill_price is None or fill_price <= 0):
            s.flags.append(f"{ticker}: fill_price<=0 with sells — lot excluded")
            anomalous = True
        if sell_anomaly:
            s.flags.append(f"{ticker}: sell with NULL price — excluded")

        if anomalous:
            continue  # excluded from all totals/stats; no by_ticker line

        sold_total = sum(q for q, _ in sells)
        proceeds = sum(q * p for q, p in sells)
        mult = OPTION_MULTIPLIER if itype == "option" else 1
        realized = (proceeds - sold_total * (fill_price or 0.0)) * mult
        is_closed = sold_total > 0
```

Note: `s = src(channel)` now appears once (moved up). Ensure the previously-existing `s = src(channel)` line lower in the loop is removed so it is not duplicated.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_pnl_attribution.py -v`
Expected: PASS (all unit tests)

- [ ] **Step 5: Commit**

```bash
git add agent/pnl_attribution.py tests/unit/test_pnl_attribution.py
git commit -m "feat(pnl): flag+exclude data anomalies (NULL price, zero-cost)"
```

---

## Task 6: CLI — fetch layer + table render

Builds `bin/pnl_report.py`: sync `sqlite3` fetch with `--channel`/`--since-entry`/`--since-sell` filtering, calls the pure core, prints a per-source table. Telegram comes in Task 7.

**Files:**
- Create: `bin/pnl_report.py`
- Test: `tests/integration/test_pnl_report_cli.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_pnl_report_cli.py
# NOTE: these are SYNC tests. bin/pnl_report.py is synchronous and calls
# asyncio.run() internally for --telegram; running them under pytest-asyncio's
# event loop would raise "asyncio.run() cannot be called from a running event
# loop". So the (async) DB seeding is driven via asyncio.run() in the fixture,
# and the tests themselves are plain `def`.
import asyncio
import pytest
from infra.storage.db import get_connection
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.position_exit_store import PositionExitStore
import bin.pnl_report as cli


async def _seed(db_path):
    """Two sources. stp: NVDA closed +100. mystic: TSLA closed -50.
    Plus an stp open option (no exit path) and a zero-fill exit (ignored)."""
    conn = await get_connection(db_path)
    intents = TradeIntentStore(conn)
    exits = PositionExitStore(conn)

    async def filled(intent_id, channel, ticker, itype, price, qty):
        await intents.insert({
            "intent_id": intent_id, "event_id": "e", "channel": channel,
            "ticker": ticker, "side": "long", "instrument_type": itype,
            "conviction": "high", "fill_price": price, "fill_qty": qty,
            "execution_state": "filled", "outbox_status": "confirmed",
            "policy_state": "approved", "signal_received_at": "2026-05-01T00:00:00+00:00",
            "intent_created_at": "2026-05-01T00:00:00+00:00",
            "filled_at": "2026-05-01T14:00:00+00:00",
            "created_at": "2026-05-01T00:00:00+00:00",
            "updated_at": "2026-05-01T00:00:00+00:00"})

    await filled("nvda", "stp", "NVDA", "equity", 100.0, 10)
    await filled("tsla", "mystic", "TSLA", "equity", 100.0, 10)
    await filled("aapl", "stp", "AAPL", "option", 2.0, 1)  # open option

    await exits.record_exit(fingerprint="f1", event_id="e", intent_id="nvda",
                            channel="stp", ticker="NVDA", scope="full",
                            requested_qty=10, sold_qty=10, sold_avg_price=110.0,
                            broker_order_ref="r1", reason="follow_sell")
    await exits.record_exit(fingerprint="f2", event_id="e", intent_id="tsla",
                            channel="mystic", ticker="TSLA", scope="full",
                            requested_qty=10, sold_qty=10, sold_avg_price=95.0,
                            broker_order_ref="r2", reason="follow_sell")
    # zero-fill exit must be ignored
    await exits.record_exit(fingerprint="f3", event_id="e", intent_id="nvda",
                            channel="stp", ticker="NVDA", scope="full",
                            requested_qty=1, sold_qty=0, sold_avg_price=None,
                            broker_order_ref=None, reason="follow_sell")
    await conn.close()


@pytest.fixture
def seeded_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    asyncio.run(_seed(db_path))
    return db_path


def test_cli_prints_per_source_totals(seeded_db, capsys):
    rc = cli.main(["--db", seeded_db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stp" in out and "mystic" in out
    assert "+100.00" in out      # NVDA realized
    assert "-50.00" in out       # TSLA realized
    assert "AAPL" in out         # open option surfaced
    assert "no exit path" in out


def test_cli_channel_filter(seeded_db, capsys):
    cli.main(["--db", seeded_db, "--channel", "stp"])
    out = capsys.readouterr().out
    assert "stp" in out
    assert "mystic" not in out


def test_cli_missing_db_exits_nonzero(tmp_path, capsys):
    rc = cli.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_pnl_report_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bin.pnl_report'`

- [ ] **Step 3: Write `bin/pnl_report.py` (fetch + table; no Telegram yet)**

```python
#!/usr/bin/env python3
"""Per-source realized P&L report. Read-only; never touches orders.

Realized P&L per trader/channel from existing stores (entries in trade_intents;
sells in trade_intent_trims + position_exits), with per-ticker / equity-vs-option
breakdowns and win-rate stats. See
docs/superpowers/specs/2026-06-02-per-source-pnl-attribution-design.md.

Usage:
    python bin/pnl_report.py
    python bin/pnl_report.py --channel stp
    python bin/pnl_report.py --since-entry 2026-05-01
    python bin/pnl_report.py --since-sell 2026-05-15
    python bin/pnl_report.py --telegram
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

from agent.pnl_attribution import compute_attribution, AttributionReport


def _fetch(db_path, *, channel=None, since_entry=None, since_sell=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = "execution_state='filled'"
        params: list = []
        if channel:
            where += " AND channel=?"
            params.append(channel)
        if since_entry:
            where += " AND filled_at>=?"
            params.append(since_entry)
        entries = conn.execute(
            "SELECT intent_id, channel, ticker, instrument_type, fill_price, "
            f"fill_qty FROM trade_intents WHERE {where}", params).fetchall()
        ids = {e["intent_id"] for e in entries}

        trims = [r for r in conn.execute(
            "SELECT intent_id, sold_qty, sold_avg_price, fired_at "
            "FROM trade_intent_trims WHERE fired_at IS NOT NULL").fetchall()
            if r["intent_id"] in ids
            and (not since_sell or (r["fired_at"] or "") >= since_sell)]
        exits = [r for r in conn.execute(
            "SELECT intent_id, sold_qty, sold_avg_price, created_at "
            "FROM position_exits").fetchall()
            if r["intent_id"] in ids
            and (not since_sell or (r["created_at"] or "") >= since_sell)]
    finally:
        conn.close()

    if since_sell:
        # In sell-window mode, only show lots that actually realized in-window.
        sold_ids = {r["intent_id"] for r in trims} | {r["intent_id"] for r in exits}
        entries = [e for e in entries if e["intent_id"] in sold_ids]
    return entries, trims, exits


def render_table(report: AttributionReport) -> str:
    if not report.sources:
        return "No realized P&L for the selected window."
    lines = []
    header = f"{'Source':<14} {'Realized':>12} {'Lots':>5} {'Win%':>6} " \
             f"{'AvgWin':>10} {'AvgLoss':>10}"
    lines.append(header)
    lines.append("-" * len(header))
    for s in report.sources:
        lines.append(
            f"{s.channel:<14} {s.realized:>+12.2f} {s.closed_lots:>5} "
            f"{s.win_rate * 100:>5.0f}% {s.avg_win:>+10.2f} {s.avg_loss:>+10.2f}")
        for l in s.by_ticker:
            lines.append(f"    {l.ticker:<10} ({l.instrument_type:<6}) "
                         f"{l.realized:>+12.2f}  [{l.closed_lots} closed]")
        if s.open_options:
            lines.append(f"    {s.open_options} open option lot(s), cost "
                         f"{s.open_option_cost:>.2f}  [open · no exit path]")
        for f in s.flags:
            lines.append(f"    ⚠ {f}")
    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':<14} {report.grand_total:>+12.2f} "
                 f"{report.total_closed_lots:>5} {report.win_rate * 100:>5.0f}%")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Per-source realized P&L report")
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--channel", default=None)
    parser.add_argument("--since-entry", default=None,
                        help="ISO date; include lots whose entry filled on/after")
    parser.add_argument("--since-sell", default=None,
                        help="ISO date; realized from sells on/after (lot must "
                             "have an in-window sell to appear)")
    parser.add_argument("--policy", default="config/policy.yaml")
    parser.add_argument("--telegram", action="store_true",
                        help="also push a compact summary to Telegram")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: db not found: {args.db}", file=sys.stderr)
        return 2

    entries, trims, exits = _fetch(
        args.db, channel=args.channel, since_entry=args.since_entry,
        since_sell=args.since_sell)
    report = compute_attribution(entries, trims, exits)
    print(render_table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_pnl_report_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bin/pnl_report.py tests/integration/test_pnl_report_cli.py
git commit -m "feat(pnl): bin/pnl_report.py CLI — fetch + per-source table"
```

---

## Task 7: Telegram push (`--telegram`)

Adds a compact HTML summary pushed via the existing `TelegramClient`, built from policy exactly like `main.py`. The CLI table always prints first; the push is additive.

**Files:**
- Modify: `bin/pnl_report.py`
- Test: `tests/integration/test_pnl_report_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/integration/test_pnl_report_cli.py

def test_render_telegram_summary_is_compact_html():
    from agent.pnl_attribution import (
        AttributionReport, SourcePnl, InstrumentBreakdown)
    s = SourcePnl(channel="stp", realized=100.0, closed_lots=1, wins=1,
                  by_instrument=InstrumentBreakdown(equity=100.0))
    report = AttributionReport(sources=[s], grand_total=100.0,
                               total_closed_lots=1, total_wins=1)
    html = cli.render_telegram(report)
    assert "stp" in html
    assert "+100.00" in html
    assert "<b>" in html  # uses HTML parse_mode markup


def test_telegram_flag_sends_summary(seeded_db, capsys, monkeypatch):
    sent = []

    class FakePolicy:
        class telegram:
            bot_token = "x"
            chat_id = "y"

    monkeypatch.setattr(cli, "load_policy", lambda path: FakePolicy())

    class FakeClient:
        def __init__(self, token, chat_id):
            pass
        async def send_message(self, text):
            sent.append(text)

    monkeypatch.setattr(cli, "TelegramClient", FakeClient)
    rc = cli.main(["--db", seeded_db, "--telegram"])
    assert rc == 0
    assert len(sent) == 1
    assert "stp" in sent[0]
    # terminal table still printed
    assert "stp" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_pnl_report_cli.py -v`
Expected: FAIL — `render_telegram` / `load_policy` / `TelegramClient` not defined in `cli`

- [ ] **Step 3: Add Telegram rendering + send to `bin/pnl_report.py`**

Add imports near the top (below the existing imports):

```python
import asyncio

from agent.policy import load_policy
from infra.telegram.client import TelegramClient
```

Add `render_telegram` after `render_table`:

```python
def render_telegram(report: AttributionReport) -> str:
    if not report.sources:
        return "<b>P&amp;L</b>: no realized P&amp;L for the selected window."
    rows = [f"<b>Realized P&amp;L by source</b>  (total "
            f"<b>{report.grand_total:+.2f}</b>, {report.total_closed_lots} lots, "
            f"{report.win_rate * 100:.0f}% win)"]
    for s in report.sources:
        rows.append(f"• <b>{s.channel}</b>: {s.realized:+.2f} "
                    f"({s.closed_lots} lots, {s.win_rate * 100:.0f}% win)")
    return "\n".join(rows)
```

Add a send helper and call it from `main()` before `return 0`:

```python
async def _send_telegram(policy_path: str, text: str) -> None:
    policy = load_policy(policy_path)
    client = TelegramClient(policy.telegram.bot_token, policy.telegram.chat_id)
    await client.send_message(text)
```

In `main()`, replace the final `print(render_table(report))\n    return 0` with:

```python
    print(render_table(report))
    if args.telegram:
        try:
            asyncio.run(_send_telegram(args.policy, render_telegram(report)))
        except Exception as exc:  # report already printed; surface send failure
            print(f"error: telegram send failed: {exc}", file=sys.stderr)
            return 3
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_pnl_report_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bin/pnl_report.py tests/integration/test_pnl_report_cli.py
git commit -m "feat(pnl): optional --telegram summary push"
```

---

## Task 8: Full suite + executable bit

**Files:**
- Modify: `bin/pnl_report.py` (mode only)

- [ ] **Step 1: Make the script executable (matches other bin scripts)**

Run: `chmod +x bin/pnl_report.py`

- [ ] **Step 2: Run the entire test suite**

Run: `pytest tests/unit/test_pnl_attribution.py tests/integration/test_pnl_report_cli.py -v`
Expected: PASS (all tasks)

- [ ] **Step 3: Smoke-test against the real DB (read-only)**

Run: `python bin/pnl_report.py --db data/trading_agent.db`
Expected: a per-source table, or "No realized P&L for the selected window." — no errors, no writes.

- [ ] **Step 4: Run the full project test suite to confirm no regressions**

Run: `pytest -q`
Expected: PASS (no new failures)

- [ ] **Step 5: Commit**

```bash
git add bin/pnl_report.py
git commit -m "chore(pnl): mark pnl_report.py executable"
```

---

## Self-Review notes (addressed)

- **Spec coverage:** realized-only (Tasks 2,5) · per-source/per-ticker/equity-vs-option/win-rate (Tasks 2–4) · options-as-$0-flagged-open (Task 3) · zero-fill + anomaly exclusion (Task 5) · CLI with all flags (Task 6) · `--telegram` via `load_policy` (Task 7) · temp-file integration DB (Task 6). All covered.
- **Type consistency:** `compute_attribution(entries, trims, exits) -> AttributionReport`; `SourcePnl` fields (`realized, closed_lots, wins, losses, avg_win, avg_loss, best_lot, worst_lot, by_instrument, by_ticker, open_options, open_option_cost, flags, win_rate`), `TickerLine(ticker, instrument_type, realized, closed_lots)`, `InstrumentBreakdown(equity, option)` used identically across tasks and tests. CLI uses `render_table`, `render_telegram`, `_fetch`, `_send_telegram`, `main(argv)`.
- **No placeholders:** every code step is complete and runnable.
