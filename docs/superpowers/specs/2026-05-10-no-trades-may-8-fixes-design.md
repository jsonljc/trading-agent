# No Trades on May 8 — Root Cause and Fixes

**Date:** 2026-05-10
**Status:** Design approved, ready for implementation plan
**Symptom:** May 8, 2026: 85 captured signals → 0 trade intents → 0 broker orders.

## Root cause summary

Five distinct issues compounded. Even with all four upstream/lossy filters working perfectly, only 4 May-8 signals would have produced trade intents — and all 4 died at a schema crash on insert.

| Where signal died | Count | Cause | Fix scope |
|---|---|---|---|
| Schema crash post-classify | 4 | Live DB missing `parent_intent_id` + `fill_qty` columns | §1 |
| EntrySkipGate (`bucket=SKIP`) | 28 | LLM correctly rejected non-entries | None — working as intended |
| TraderRouter `no_trader_profile:The Pup of Wall St` | 23 | `pup-danny.yaml` pattern doesn't match actual author | §2 |
| TraderRouter `no_trader_profile:` (empty author) | 24 | Discord groups consecutive messages; extension's username selector returns null | §3 |
| TraderRouter `missing_alert_mention` | 6 | `require_alert_mention=true` filtered correctly | None — §4 no-op |

## §1 — Schema migration

`infra/storage/db.py:218` `_migrate()` only migrates `partial_execution_reason` and `fire_started_at`. The SCHEMA string at line 139–140 was extended with `fill_qty` and `parent_intent_id`, but `CREATE TABLE IF NOT EXISTS` does not patch existing tables. Every options-sleeve insert via `OptionsMarketSubmitter` has crashed with `sqlite3.OperationalError: table trade_intents has no column named parent_intent_id` since 2026-05-06.

**Fix:**

```python
async def _migrate(conn: aiosqlite.Connection) -> None:
    await _add_column_if_missing(conn, "trade_intents", "partial_execution_reason", "TEXT")
    await _add_column_if_missing(conn, "trade_intent_trims", "fire_started_at", "TEXT")
    await _add_column_if_missing(conn, "trade_intents", "fill_qty", "INTEGER")
    await _add_column_if_missing(conn, "trade_intents", "parent_intent_id", "TEXT")
```

Idempotent. Picks up on next agent boot.

**Drift guardrail (regression test):** add a unit test that:

1. Builds a connection against a temp DB with an *older* schema snapshot (pre-fill_qty / pre-parent_intent_id).
2. Runs `_migrate()`.
3. Asserts the resulting column set on every table matches the columns implied by the live SCHEMA string.

This catches the class of bug where SCHEMA grows but `_migrate()` is forgotten.

## §2 — `pup-danny` pattern + audit

**(a) Fix the YAML.** `config/traders/pup-danny.yaml`:

```yaml
discord_author_pattern: "The Pup of Wall St"   # was: "Pup Danny"
```

**(b) Audit results across all captured signals (full history):**

| Trader | Configured pattern | Actual author(s) seen | Status |
|---|---|---|---|
| pup-danny | `Pup Danny` | "The Pup of Wall St" (36) | **broken — fix above** |
| urkel | `Urkel` | "Urkel" (26) | ok |
| stocktalkweekly | `Stock Talk Weekly` | "Stock Talk Weekly" (45) | ok |
| wallstengine | `Wall St Engine` | "Wall St Engine" (16 across two channel slugs) | ok |
| mystic | `UndefinedMystic` | "UndefinedMystic" (19) | ok |

**(c) Operator audit script:** `bin/audit_trader_patterns.py` — joins `signal_events` by channel, lists configured pattern vs. observed author distribution, flags any trader whose pattern matches < 50% of messages in its mapped channel(s). One-shot; run on demand.

**Out of scope (flagged for future):** channel-slug split (`wallstengine` vs `wall-st-engine`, `stocktalkweekly` vs `stock-talk-portfolio`) reflects evolution of the channel-id map over time. Doesn't affect routing today since both slugs map to the same trader's pattern, but worth a separate cleanup.

## §3 — Empty-author capture

**Root cause.** Discord groups consecutive messages from the same author into a visual cluster; only the first message in the cluster renders the `[class*="username"]` element. `extension/extract.js:19-20` returns `""` for every grouped message. Empirical evidence: pup-danny channel has 36 author-bearing rows + 34 empty (≈ 1:1, heavy back-to-back chatter); stocktalkweekly has 45 + 2 (clean alert posts, no clustering).

**Channel-based fallback was rejected.** pup-danny channel hosts both "The Pup of Wall St" and "Naz" — channel→trader inference would mis-attribute Naz continuations and trigger trades on his ideas. The fix must recover the *real* author.

**Fix in `extension/extract.js`:** when `usernameEl` is null, walk previous siblings (skipping non-message elements like date dividers) until finding a `chat-messages-…` element whose username node exists; use that author.

```js
let usernameEl = el.querySelector('[class*="username"]');
let cursor = el;
while (!usernameEl && cursor.previousElementSibling) {
  cursor = cursor.previousElementSibling;
  if (cursor.id && cursor.id.startsWith("chat-messages-")) {
    usernameEl = cursor.querySelector('[class*="username"]');
  }
}
const author = usernameEl ? usernameEl.innerText.trim() : "";
```

**Edge cases:**
- **Date dividers and other non-message siblings:** loop steps over them via the id-prefix guard.
- **Top of viewport (Discord lazy-renders):** returns empty, drops at router same as today. Acceptable — on next scroll the message would render with full context but we don't re-emit. Frequency should be low; track via existing `no_trader_profile:` log if it becomes an issue.
- **Cluster boundary (Naz follows Pup):** each cluster starts with its own username-bearing message; walk-up stops at the right one.

**Test:** add jsdom-based unit test for `extractMessage` covering: (1) standalone message with username, (2) grouped second-message with no username (asserts author resolved from prior sibling), (3) grouped message across a date-divider sibling, (4) message at top with no prior cluster (asserts empty author).

**Subsumes §4 sub-issue:** the 3 stocktalkweekly rows where `author = "@Stock Talk Weekly - Alerts"` are the same DOM bug — the role-mention element matched the username selector for a grouped message. After §3, the prior cluster member's real author resolves correctly. Verify after deploy.

## §4 — Alert-mention strictness

**No code change.** `require_alert_mention` is correct by design: stocktalkweekly's channel mixes alerts and commentary, and only alert-tagged messages are intended as actionable signals. Relaxing the literal-substring match risks generating false trades.

The 6 May-8 missing-mention skips are working as intended. The "@Stock Talk Weekly - Alerts" as author rows are subsumed by §3.

**Deferred:** if we later see false-negatives on real alerts (e.g. due to curly quotes, em-dashes, role-id changes), revisit with a normalized-substring match. Not now.

## Implementation order

1. **§1 schema migration** — highest impact, unblocks every options write. Ship first, on its own.
2. **§2 (a) pup-danny YAML** — one-line config fix. Trivial.
3. **§3 extension fix** — recovers ~30%+ of lost signals. Requires extension reload by user.
4. **§2 (c) audit script** — operator tool, no production effect.
5. **§1 drift guardrail test + §3 jsdom test** — bundle as the test layer.

§4 contributes nothing.

## Verification plan

After each fix lands:

- §1: tail `logs/agent.log` for `parent_intent_id` errors — should disappear. Confirm a successful options-sleeve write inserts a row with non-null `parent_intent_id` in `trade_intents`.
- §2 (a): inspect `skill_outputs` for next pup-danny signal — TraderRouter status should be `success` with `trader_handle=pup-danny`.
- §3: replay a known grouped-message scenario in Discord; capture output via the extension; confirm author non-empty for second message in cluster. Then in production, audit `signal_events` empty-author rate over a 24h window — should drop near zero.
- §4: no action.
