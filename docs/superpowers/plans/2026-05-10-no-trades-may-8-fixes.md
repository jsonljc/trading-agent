# No-Trades-on-May-8 Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore end-to-end signal-to-order execution. After this plan, a Discord message from a tracked trader (e.g. `OPEN MP 75C` from Pup Danny) results in: signal captured → trade intent persisted → 10% (or per-channel/conviction-tuned) shares MKT order placed at IB Gateway → options sleeve attempted → fill row updated.

**Architecture:** Surgical fixes at four pre-existing failure points along the captured signal pipeline; no architectural change. Order: schema migration (unblocks every options write), trader YAML pattern (recovers highest-volume channel), Chrome-extension author recovery (recovers ~30% of all signals), audit tooling and drift regression test (prevents recurrence).

**Tech Stack:** Python 3.11+ (`aiosqlite`, `pytest-asyncio`), SQLite, Chrome MV3 content script (vanilla JS), existing `agent.orchestrator` pipeline.

**Reference spec:** `docs/superpowers/specs/2026-05-10-no-trades-may-8-fixes-design.md`

---

## File map

| Path | Change | Responsibility |
|---|---|---|
| `infra/storage/db.py` | Modify `_migrate()` (line 215-219) | Add `fill_qty` and `parent_intent_id` migrations to `trade_intents` |
| `tests/unit/test_schema_migration_drift.py` | Create | Regression: live SCHEMA columns must all exist after `_migrate()` runs over an old DB |
| `config/traders/pup-danny.yaml` | Modify line 3 | Match actual Discord author "The Pup of Wall St" |
| `bin/audit_trader_patterns.py` | Create | Operator script: report each trader's configured pattern vs. observed authors in `signal_events` |
| `extension/extract.js` | Modify lines 19-20 | Walk previous siblings to recover author for grouped messages |
| `extension/test/harness.html` | Modify | Add 4 grouped-message cases to existing browser-based harness |

No file becomes large enough to need splitting; the design is a set of point-edits.

---

## Task 1: Schema drift regression test (TDD — write failing test first)

**Files:**
- Test (create): `tests/unit/test_schema_migration_drift.py`

This test deliberately runs first. It asserts the *invariant* that `_migrate()` keeps live DBs in sync with the SCHEMA string. We need it to fail today (because `fill_qty` and `parent_intent_id` are missing from `_migrate()`), then pass after Task 2.

- [ ] **Step 1: Create the failing test**

```python
# tests/unit/test_schema_migration_drift.py
"""
Regression for the May-8 incident: SCHEMA in db.py grew columns
(fill_qty, parent_intent_id) but _migrate() was not updated, so live DBs
went stale and every options-sleeve insert crashed with
'OperationalError: table trade_intents has no column named parent_intent_id'.

This test simulates an old live DB by pre-creating trade_intents WITHOUT
the new columns, then runs get_connection() (which calls _migrate()), then
asserts the column set matches what the live SCHEMA defines.
"""
from __future__ import annotations
import re
import pytest
import aiosqlite
from infra.storage.db import get_connection, SCHEMA


def _expected_columns_from_schema(table: str) -> set[str]:
    """Parse columns out of the SCHEMA string for a given table."""
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\)",
        SCHEMA, re.DOTALL,
    )
    assert m, f"could not find {table} in SCHEMA"
    body = m.group(1)
    cols: set[str] = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        if line.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK")):
            continue
        # First token on the line is the column name.
        name = line.split()[0]
        cols.add(name)
    return cols


async def _live_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_migrate_brings_old_db_up_to_current_schema(tmp_path):
    db_path = tmp_path / "old.db"

    # Simulate a pre-fill_qty / pre-parent_intent_id DB.
    OLD_TRADE_INTENTS = """
    CREATE TABLE trade_intents (
        intent_id              TEXT PRIMARY KEY,
        event_id               TEXT NOT NULL,
        channel                TEXT NOT NULL,
        ticker                 TEXT NOT NULL,
        side                   TEXT NOT NULL,
        instrument_type        TEXT NOT NULL,
        conviction             TEXT NOT NULL,
        policy_state           TEXT NOT NULL,
        signal_received_at     TEXT NOT NULL,
        intent_created_at      TEXT NOT NULL,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL
    );
    """
    pre = await aiosqlite.connect(str(db_path))
    try:
        await pre.executescript(OLD_TRADE_INTENTS)
        await pre.commit()
    finally:
        await pre.close()

    # Now open through the production code path (runs _migrate()).
    conn = await get_connection(str(db_path))
    try:
        live = await _live_columns(conn, "trade_intents")
        expected = _expected_columns_from_schema("trade_intents")
        missing = expected - live
        assert not missing, (
            f"_migrate() did not add columns expected by SCHEMA: {sorted(missing)}. "
            f"Add _add_column_if_missing(...) calls in infra/storage/db.py::_migrate."
        )
    finally:
        await conn.close()
```

- [ ] **Step 2: Run the test and verify it fails**

```
pytest tests/unit/test_schema_migration_drift.py -v
```

Expected: FAIL. The error message should list `{'fill_qty', 'parent_intent_id'}` as missing.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/unit/test_schema_migration_drift.py
git commit -m "test: regression for trade_intents schema drift (currently failing)"
```

---

## Task 2: Schema migration fix (make Task 1 pass)

**Files:**
- Modify: `infra/storage/db.py:215-219`

- [ ] **Step 1: Add the two missing migrations**

Edit `infra/storage/db.py` `_migrate` to add the two missing columns. Replace lines 215-219:

```python
async def _migrate(conn: aiosqlite.Connection) -> None:
    """Idempotent ALTERs for columns added after first deploy. SQLite's
    CREATE TABLE IF NOT EXISTS does not patch existing tables."""
    await _add_column_if_missing(conn, "trade_intents", "partial_execution_reason", "TEXT")
    await _add_column_if_missing(conn, "trade_intent_trims", "fire_started_at", "TEXT")
    await _add_column_if_missing(conn, "trade_intents", "fill_qty", "INTEGER")
    await _add_column_if_missing(conn, "trade_intents", "parent_intent_id", "TEXT")
```

- [ ] **Step 2: Run the regression test and verify it passes**

```
pytest tests/unit/test_schema_migration_drift.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the full unit test suite to make sure nothing else regressed**

```
pytest tests/unit -q
```

Expected: all green.

- [ ] **Step 4: Migrate the live DB by reopening the connection (start agent or run a migrate script)**

The simplest path: stop the agent, restart it; `get_connection()` runs `_migrate()` on boot. Verify columns are present:

```
sqlite3 data/trading_agent.db "PRAGMA table_info(trade_intents);" | grep -E "fill_qty|parent_intent_id"
```

Expected: two lines printed, one each for `fill_qty INTEGER` and `parent_intent_id TEXT`.

If the agent is running and you don't want to restart yet, run the migration via a one-shot Python:

```
python -c "import asyncio; from infra.storage.db import get_connection; asyncio.run(get_connection('data/trading_agent.db')).close()"
```
(Note: closing matters; the connection runs `_migrate()` then the script exits.)

- [ ] **Step 5: Commit**

```bash
git add infra/storage/db.py
git commit -m "fix(schema): migrate trade_intents to add fill_qty + parent_intent_id

Was: SCHEMA in db.py was extended in commit c31bbc8 but _migrate()
was not updated. Live DBs stayed on the old shape, and every
OptionsMarketSubmitter insert crashed with OperationalError. Caused
0 trade intents on 2026-05-08 despite 85 captured signals."
```

---

## Task 3: Fix `pup-danny` author pattern

**Files:**
- Modify: `config/traders/pup-danny.yaml:3`

This is a one-line YAML edit. Recovers 23+ May-8 signals immediately upon agent restart.

- [ ] **Step 1: Edit the YAML**

In `config/traders/pup-danny.yaml`, change line 3:

```yaml
discord_author_pattern: "The Pup of Wall St"
```
(was `"Pup Danny"`.)

- [ ] **Step 2: Add a sanity unit test that the YAML loads and the new pattern matches the captured-author shape**

Create `tests/unit/test_pup_danny_pattern.py`:

```python
from pathlib import Path
from agent.traders.profile import load_profile

def test_pup_danny_pattern_matches_captured_author():
    p = load_profile(Path("config/traders/pup-danny.yaml"))
    assert p.discord_author_pattern == "The Pup of Wall St", (
        "If you re-renamed this, also update the audit notes; the captured "
        "Discord author for this channel is 'The Pup of Wall St', not "
        "'Pup Danny' (display_name) and not the channel slug."
    )
```

- [ ] **Step 3: Run it**

```
pytest tests/unit/test_pup_danny_pattern.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add config/traders/pup-danny.yaml tests/unit/test_pup_danny_pattern.py
git commit -m "fix(trader): pup-danny pattern matches actual Discord author

Captured author for this channel is 'The Pup of Wall St'; YAML
incorrectly used the trader handle 'Pup Danny'. Cost 23 May-8 signals
at TraderRouter (no_trader_profile:The Pup of Wall St)."
```

---

## Task 4: Trader-pattern audit script

**Files:**
- Create: `bin/audit_trader_patterns.py`

A one-shot operator tool. No production-runtime impact. Reports configured pattern vs. observed author distribution per channel so future drift is one command away.

- [ ] **Step 1: Write the script**

Create `bin/audit_trader_patterns.py`:

```python
#!/usr/bin/env python3
"""
Audit each trader profile's discord_author_pattern against the authors
actually captured in signal_events. Prints a table; exits non-zero if
any tracked channel has < 50% of its signals matched by the configured
pattern (drift detector).

Usage:
    python bin/audit_trader_patterns.py
    python bin/audit_trader_patterns.py --since 2026-04-15
    python bin/audit_trader_patterns.py --db data/trading_agent.db
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import yaml


def load_profiles(directory: Path) -> dict[str, dict]:
    """handle -> {pattern, channel_hint}. channel_hint is the YAML basename."""
    out: dict[str, dict] = {}
    for p in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text())
        out[raw["handle"]] = {
            "pattern": raw["discord_author_pattern"],
            "channel_slug_hint": p.stem,
        }
    return out


def channels_for_handle(channel_id_map: dict[str, str], handle: str) -> set[str]:
    return {slug for _id, slug in channel_id_map.items() if slug == handle}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/trading_agent.db")
    parser.add_argument("--since", default=None,
                        help="ISO date, e.g. 2026-04-15. Default: all time.")
    parser.add_argument("--policy", default="config/policy.yaml")
    parser.add_argument("--traders", default="config/traders")
    args = parser.parse_args()

    policy = yaml.safe_load(Path(args.policy).read_text())
    channel_id_map = policy.get("discord_extension", {}).get("channel_id_map", {})
    profiles = load_profiles(Path(args.traders))

    where = "WHERE 1=1"
    params: list[str] = []
    if args.since:
        where += " AND received_at >= ?"
        params.append(args.since)

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        f"SELECT channel, author, COUNT(*) FROM signal_events {where} "
        "GROUP BY channel, author ORDER BY channel, COUNT(*) DESC",
        params,
    ).fetchall()
    conn.close()

    by_channel: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for ch, au, n in rows:
        by_channel[ch].append((au or "", n))

    fail = False
    print(f"{'Trader':<18} {'Pattern':<28} {'Channel':<22} {'Match':>6} {'Misses':>50}")
    print("-" * 130)
    for handle, info in profiles.items():
        pattern = info["pattern"]
        channels = channels_for_handle(channel_id_map, handle)
        if not channels:
            channels = {info["channel_slug_hint"]}
        for ch in sorted(channels):
            counts = by_channel.get(ch, [])
            total = sum(n for _, n in counts)
            matched = sum(n for au, n in counts if au == pattern)
            non_match = [(au, n) for au, n in counts if au != pattern and n > 0]
            non_match.sort(key=lambda x: -x[1])
            misses = ", ".join(f"{au!r}={n}" for au, n in non_match[:5]) or "-"
            pct = (matched / total * 100) if total else 0.0
            tag = "OK"
            if total == 0:
                tag = "no-data"
            elif pct < 50:
                tag = "DRIFT"
                fail = True
            print(f"{handle:<18} {pattern!r:<28} {ch:<22} "
                  f"{matched:>3}/{total:<3} ({pct:5.1f}%) [{tag}]  {misses}")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Make it executable and run it against the live DB**

```
chmod +x bin/audit_trader_patterns.py
python bin/audit_trader_patterns.py
```

Expected: a table printed. After Task 3 has shipped, all five tracked traders should show OK (≥ 50% match) — no `[DRIFT]` rows. If `[DRIFT]` appears, the YAML is wrong; fix and rerun before continuing.

- [ ] **Step 3: Commit**

```bash
git add bin/audit_trader_patterns.py
git commit -m "tool: audit_trader_patterns.py — detect YAML/captured-author drift

Reports configured discord_author_pattern vs. observed authors per
channel from signal_events. Exits non-zero if any channel has < 50%
match. Run after any trader-config change."
```

---

## Task 5: Chrome extension — recover author for grouped messages

**Files:**
- Modify: `extension/extract.js:19-20`
- Modify: `extension/test/harness.html` (add grouped-message cases)

Discord groups consecutive same-author messages and only renders the username on the first. The `[class*="username"]` selector returns null on the rest, producing `author=""`. Walk previous siblings (skipping date dividers and other non-`chat-messages-…` elements) until a username-bearing message is found, and use that author.

- [ ] **Step 1: Add failing harness cases first**

Edit `extension/test/harness.html`. Add these templates after the existing `case-not-message` template (before the `<script>` block):

```html
<template id="case-grouped-second">
  <ol>
    <li id="chat-messages-111111-100">
      <div>
        <span class="username-h_Y3Us">The Pup of Wall St</span>
        <time datetime="2026-05-08T14:46:00.000Z">2:46 PM</time>
        <div id="message-content-100">OPEN TSLA 500C 6/18</div>
      </div>
    </li>
    <li id="chat-messages-111111-101">
      <div>
        <time datetime="2026-05-08T14:49:00.000Z">2:49 PM</time>
        <div id="message-content-101">OPEN QCOM 250C 5/15 FILL: $3.2</div>
      </div>
    </li>
  </ol>
</template>

<template id="case-grouped-across-divider">
  <ol>
    <li id="chat-messages-222222-200">
      <div>
        <span class="username-x">Urkel</span>
        <time datetime="2026-05-08T13:56:00.000Z">1:56 PM</time>
        <div id="message-content-200">SWING ADD MU</div>
      </div>
    </li>
    <li class="divider-some-hash"><span>May 8, 2026</span></li>
    <li id="chat-messages-222222-201">
      <div>
        <time datetime="2026-05-08T14:03:00.000Z">2:03 PM</time>
        <div id="message-content-201">Mixed bag out there for swings</div>
      </div>
    </li>
  </ol>
</template>

<template id="case-cluster-boundary">
  <!-- Pup cluster, then Naz cluster. Naz's second message must NOT
       inherit Pup's author. -->
  <ol>
    <li id="chat-messages-333333-300">
      <div>
        <span class="username-x">The Pup of Wall St</span>
        <time datetime="2026-05-08T15:00:00.000Z">3:00 PM</time>
        <div id="message-content-300">QCOM nHOD</div>
      </div>
    </li>
    <li id="chat-messages-333333-301">
      <div>
        <span class="username-x">Naz</span>
        <time datetime="2026-05-08T15:01:00.000Z">3:01 PM</time>
        <div id="message-content-301">interesting</div>
      </div>
    </li>
    <li id="chat-messages-333333-302">
      <div>
        <time datetime="2026-05-08T15:02:00.000Z">3:02 PM</time>
        <div id="message-content-302">following</div>
      </div>
    </li>
  </ol>
</template>

<template id="case-top-of-viewport">
  <!-- No prior sibling with a username (Discord lazy-rendered above the fold). -->
  <ol>
    <li id="chat-messages-444444-400">
      <div>
        <time datetime="2026-05-08T16:00:00.000Z">4:00 PM</time>
        <div id="message-content-400">orphaned message</div>
      </div>
    </li>
  </ol>
</template>
```

Now extend the test script block at the bottom (just before `</script>`):

```javascript
function loadList(id) {
  const ol = document.getElementById(id).content.firstElementChild.cloneNode(true);
  return ol;  // caller picks the <li> by index
}

// Grouped second message: must inherit author from prior sibling
const g = loadList("case-grouped-second");
const second = DiscordExtract.extractMessage(g.children[1]);
check("grouped second-message author resolved",
      second && second.author === "The Pup of Wall St");
check("grouped second-message id intact",
      second && second.message_id === "101");

// Grouped across a divider element
const gd = loadList("case-grouped-across-divider");
const continuationAcrossDivider = DiscordExtract.extractMessage(gd.children[2]);
check("author resolved across non-message sibling (divider)",
      continuationAcrossDivider && continuationAcrossDivider.author === "Urkel");

// Cluster boundary: Naz continuation must inherit Naz, NOT prior cluster
const cb = loadList("case-cluster-boundary");
const nazContinuation = DiscordExtract.extractMessage(cb.children[2]);
check("cluster-boundary attribution stays with new author",
      nazContinuation && nazContinuation.author === "Naz");

// Top of viewport: no prior sibling, accept empty author (current fallthrough)
const t = loadList("case-top-of-viewport");
const orphan = DiscordExtract.extractMessage(t.children[0]);
check("orphan at top of viewport returns empty author (acceptable)",
      orphan && orphan.author === "");
```

- [ ] **Step 2: Open the harness in a browser and confirm new cases FAIL**

```
open extension/test/harness.html
```

Expected: the four new cases (`grouped second-message author resolved`, `grouped second-message id intact`, `author resolved across non-message sibling (divider)`, `cluster-boundary attribution stays with new author`) should display **FAIL** in red. The existing four should still PASS. The `orphan at top of viewport` case should PASS (existing behavior already returns `""` for empty author).

- [ ] **Step 3: Implement the sibling-walk fix in `extract.js`**

In `extension/extract.js`, replace the body of `extractMessage`'s author-extraction (current lines 19-20):

```javascript
let usernameEl = el.querySelector('[class*="username"]');
let cursor = el;
while (!usernameEl && cursor.previousElementSibling) {
  cursor = cursor.previousElementSibling;
  // Only inspect actual chat messages; skip dividers and other siblings.
  if (cursor.id && cursor.id.startsWith("chat-messages-")) {
    usernameEl = cursor.querySelector('[class*="username"]');
  }
}
const author = usernameEl ? usernameEl.innerText.trim() : "";
```

The full updated file (for clarity):

```javascript
// Pure DOM extraction. Given a Discord message <li> element, returns a
// {message_id, author, content, timestamp} object, or null if the element
// doesn't look like a renderable message.
//
// Selectors are attribute-based ([id^=...], [class*=...]) because Discord
// rotates class hashes on every release.
//
// Author resolution: Discord groups consecutive same-author messages and
// only renders the username on the first message in the cluster. For
// grouped continuations we walk previous siblings until we find a
// chat-messages-... element with a username node and inherit that author.
(function (root) {
  function extractMessage(el) {
    if (!el || !el.id || !el.id.startsWith("chat-messages-")) return null;

    const parts = el.id.split("-");
    const message_id = parts[parts.length - 1];
    if (!message_id) return null;

    const contentEl = el.querySelector('[id^="message-content-"]');
    const content = contentEl ? contentEl.innerText : "";

    let usernameEl = el.querySelector('[class*="username"]');
    let cursor = el;
    while (!usernameEl && cursor.previousElementSibling) {
      cursor = cursor.previousElementSibling;
      if (cursor.id && cursor.id.startsWith("chat-messages-")) {
        usernameEl = cursor.querySelector('[class*="username"]');
      }
    }
    const author = usernameEl ? usernameEl.innerText.trim() : "";

    const timeEl = el.querySelector("time");
    const timestamp = timeEl ? timeEl.getAttribute("datetime") : "";

    return { message_id, author, content, timestamp };
  }

  function channelIdFromUrl(url) {
    const m = url.match(/\/channels\/(\d+)\/(\d+)/);
    if (!m) return { server_id: "", channel_id: "" };
    return { server_id: m[1], channel_id: m[2] };
  }

  root.DiscordExtract = { extractMessage, channelIdFromUrl };
})(typeof window !== "undefined" ? window : globalThis);
```

- [ ] **Step 4: Reload `harness.html` in the browser; confirm all 8 cases PASS**

```
open extension/test/harness.html
```

Expected: every line shows PASS in green. If any FAIL, do not proceed — diagnose and fix before commit.

- [ ] **Step 5: Reload the extension in Chrome to pick up the new `extract.js`**

In Chrome: open `chrome://extensions`, find the Discord capture extension, click the reload (↻) icon. Then reload any open Discord tabs.

- [ ] **Step 6: Commit**

```bash
git add extension/extract.js extension/test/harness.html
git commit -m "fix(extension): resolve author for grouped Discord messages

Discord renders username only on the first message of a same-author
cluster; the [class*=username] selector returned null for grouped
continuations, producing author=\"\". Walk previous chat-messages-...
siblings (skipping dividers) to inherit the cluster author. Adds 4
harness cases including a cluster-boundary test that verifies a new
author is NOT mis-inherited from the prior cluster.

Cost on 2026-05-08: 24 captured signals dropped at TraderRouter with
no_trader_profile:."
```

---

## Task 6: End-to-end smoke verification

The point of this plan is *trades actually firing*. This task verifies that on the live system, after the four fixes, a real captured signal produces a `trade_intents` row with a non-null `broker_order_ref` (i.e. an order was actually placed at IB Gateway, paper account).

This task uses the existing `inject_event.py` harness to replay a synthetic but realistic signal without depending on live Discord posts.

- [ ] **Step 1: Restart the agent so the schema migration runs and trader registry reloads**

```
bin/agent-stop && bin/agent-start
```

Wait ~5s for boot, then check status:

```
bin/agent-status
```

Expected: status shows the agent process running, no immediate errors.

- [ ] **Step 2: Verify the schema migration actually took**

```
sqlite3 data/trading_agent.db "PRAGMA table_info(trade_intents);" | grep -E "fill_qty|parent_intent_id"
```

Expected: two lines printed (one for each column).

If empty: the agent didn't migrate (perhaps connecting to a different DB path). Inspect `config/policy.yaml` and `main.py` for the actual db path, then re-run the migration command from Task 2 Step 4 against the correct file.

- [ ] **Step 3: Inspect `inject_event.py` to confirm its CLI shape**

```
python inject_event.py --help
```

Read the help output before crafting the synthetic event in Step 4. The command's flags drive the next step; if the script's interface differs from what we draft below, adapt the Step-4 invocation accordingly (e.g. flag names, JSON-vs-args input format).

- [ ] **Step 4: Inject a synthetic Pup Danny entry signal during RTH**

This step assumes the agent is currently inside RTH (09:30–16:00 ET). If outside RTH, either time-shift the test (run during market hours) or temporarily stub `RthEntryGuard` for the test — recommended: just run during RTH so we exercise the live path.

Construct a synthetic event matching what the Chrome extension would send. Using the inject script:

```
python inject_event.py \
  --channel pup-danny \
  --author "The Pup of Wall St" \
  --message "OPEN ORCL test signal - smoke verification"
```

(If `inject_event.py` takes JSON on stdin instead, adapt accordingly per Step 3's help output.)

Expected: agent log shows `Pipeline ... TraderRouter: success`, `TraderClassifier: success`, `EntrySkipGate: success`, `TradeIntentWriter: created intent ...`, `SharesMarketSubmitter: ...`, ending in either `success` (filled) or a known partial state (e.g. `options_not_filled:...` for the options leg, which is acceptable — the shares leg is what counts).

- [ ] **Step 5: Verify a trade intent row exists with a broker order reference**

```
sqlite3 data/trading_agent.db \
  "SELECT intent_id, ticker, side, conviction, instrument_type, execution_state, fill_price, fill_qty, broker_order_ref FROM trade_intents ORDER BY intent_created_at DESC LIMIT 5;"
```

Expected: the most recent row shows `ticker=ORCL`, `instrument_type=equity`, `execution_state=filled` (or `submitted` if fill is still pending), and **a non-null `broker_order_ref`**. `broker_order_ref` non-null is the key signal — it proves the order reached IB Gateway. If `instrument_type=option` row also exists with `parent_intent_id` pointing to the equity intent, the options sleeve also fired (bonus).

If `broker_order_ref` is null and `execution_state` is something like `failed` or absent: read `logs/agent.log` to find the failure reason and stop here — there's a downstream issue beyond the four fixes that this plan addresses, and a follow-up brainstorm is warranted before claiming success.

- [ ] **Step 6: Run the audit script one more time as a final regression check**

```
python bin/audit_trader_patterns.py --since 2026-05-01
```

Expected: all five tracked traders show `[OK]`, no `[DRIFT]` rows.

- [ ] **Step 7: Commit verification artifacts (if any) and finish**

If you captured logs or notes during smoke testing in `docs/`, commit them. Otherwise no commit needed for this verification task.

---

## Out of scope (flagged for later)

- **Channel-slug split** (`wallstengine` vs `wall-st-engine`, `stocktalkweekly` vs `stock-talk-portfolio`). Both slugs currently map to the same trader pattern, so routing isn't broken — but the duplicate suggests historical evolution of the channel-id map worth cleaning. Separate plan.
- **Alert-mention strictness** (§4 of the spec): no-op by design. Revisit only if real alerts get rejected for non-substantive reasons (curly quotes, em-dashes).
- **`Naz` sender in `pup-danny` channel**: appears in extension log but isn't a tracked trader. Currently correctly skipped at TraderRouter (`no_trader_profile:Naz`). Decide later whether to silence the log entry.

## Definition of done

- All commits in this plan land on `master`.
- `pytest tests/unit -q` passes.
- `python bin/audit_trader_patterns.py` returns 0 and shows no `[DRIFT]` rows.
- A manual smoke replay produces a `trade_intents` row with a non-null `broker_order_ref` and `execution_state in ('submitted', 'filled')`.
- `logs/agent.log` shows zero `OperationalError` entries during a 30-minute observation window after restart.
