# Per-Source P&L Attribution — Design (Deferred item #1)

**Goal:** See which trader/channel sources actually make money. Compute
**realized** P&L per source from data already in the stores, broken down per-ticker
and by instrument type, with basic win/loss stats. Surface it as an on-demand CLI
report and an optional Telegram push.

**Branch:** `feat/pnl-attribution` (off master HEAD). TDD, one commit per task.
Paper mode only. Read-only — this feature never places, modifies, or cancels orders.

---

## Scope & accounting model

- **Realized P&L only.** Zero market-data dependency — everything is derivable from
  existing rows. Unrealized / mark-to-market is explicitly out of scope (it would
  pull in live market data, which is constrained).
- **Per-lot accounting**, and unambiguous: every sell is already keyed to a specific
  entry `intent_id`, so there is no FIFO / average-cost matching to resolve. Each
  filled entry intent IS one lot.
- **Lots = every filled `trade_intents` row, equity AND option.** Options are written
  as *child* rows (`instrument_type='option'`, `parent_intent_id` = the shares
  intent_id) by `OptionsMarketSubmitter`. They are real lots with their own cost
  basis and MUST be included. **Do NOT filter `parent_intent_id IS NULL`** — that
  would silently drop every options position. (Child rows carry their own `channel`,
  so per-source grouping is correct for them too.) Shorts never fill
  (`SharesMarketSubmitter` skips `side='short'`), so all filled entries are `long`;
  no side filtering is needed.
- Sells for a lot come from **two** tables, both keyed by the entry `intent_id`:
  - `trade_intent_trims` — the +5%/+10% upside trim-ladder sells (`sold_qty`,
    `sold_avg_price`, fire timestamp `fired_at`).
  - `position_exits` — Phase E follow-sells (`sold_qty`, `sold_avg_price`,
    `created_at`).
  These are **disjoint** write paths — no double-counting risk (verified:
  `exit_ladder.py` writes only `trade_intent_trims`; `sell_follower.py` writes only
  `position_exits`; neither creates a child `trade_intents` row).
- **Realized formula** per entry intent:
  `realized = Σ(sold_qty × sold_avg_price) − (Σ sold_qty × entry.fill_price)`,
  summed across that intent's trims + exits. Multiply by the contract multiplier
  (100, always — not stored per row) for `instrument_type='option'`.
- **A "sell" counts only when `sold_qty > 0` AND `sold_avg_price` is non-NULL.**
  `position_exits` records zero-fill follow-sells as `sold_qty=0` (audit trail);
  a trim/exit can also carry a NULL avg price. Such rows are **excluded entirely**
  from realized math and from "closed lot" determination — they neither add proceeds
  nor mark a lot closed.
- **Data-quality anomalies** are excluded from the math and **flagged in output**
  (never booked as phantom P&L): a sell with `sold_qty > 0` but NULL `sold_avg_price`,
  and an entry with `fill_price <= 0` (shares submitter writes `avg_fill_price or 0.0`,
  so a 0.0 cost basis is possible). The lot/source is marked with a `⚠ data` flag and
  its anomalous component is left out of the totals rather than fabricating a gain/loss.
- **Options realize $0 today.** The trim ladder qualifies equity only
  (`gw.qualify_equity`) and sell-following is shares-only, so options currently have
  **no exit path**. They are reported with cost basis + held qty and an explicit
  `[open · no exit path]` marker — surfaced, not silently dropped.
- **Paper mode**: commissions / fees are ignored (not stored anywhere).

## Breakdowns

Grouped by **source** (`channel` == trader handle, per the capture convention):

1. **Per-source total** — realized $ across all that source's lots.
2. **Per-ticker within source** — realized $ per (source, ticker).
3. **Equity vs options split** — realized $ per (source, instrument_type). Options
   line shows $0 realized + open-qty marker.
4. **Stats per source** — closed-lot count, win rate, avg win, avg loss, best lot,
   worst lot.

Definitions:
- A **closed lot** = an entry intent with any realized sells (`Σ sold_qty > 0`).
  (A partially-sold lot counts; its realized P&L is on the sold portion only.)
- **Win** = a closed lot whose realized P&L > 0. **Win rate** = wins / closed lots.
- A lot with `realized == 0` exactly (rare; e.g. sold at cost) counts as neither
  win nor loss in avg-win / avg-loss but IS a closed lot.

## Architecture — one pure core, two thin surfaces

```
                ┌───────────────────────────────┐
  sqlite rows → │ agent/pnl_attribution.py       │ → AttributionReport
   (dicts)      │   compute_attribution(...)     │   (pure data)
                │   — pure, no DB, no I/O         │
                └───────────────────────────────┘
                      ▲                    │
        fetch rows    │                    │ format
   ┌──────────────────┴───┐       ┌────────┴───────────────┐
   │ bin/pnl_report.py    │ ────▶ │ terminal table (default)│
   │ sync sqlite3 + argparse      │ Telegram HTML (--telegram)│
   └──────────────────────┘       └────────────────────────┘
```

### `agent/pnl_attribution.py` — the accounting brain (pure)
- `compute_attribution(entries, trims, exits) -> AttributionReport` where the three
  args are plain lists of dict-like rows (sqlite3.Row or dict — accessed by key).
- Returns a structured, JSON-friendly report: per-source records each containing the
  total, per-ticker lines, per-instrument lines, and stats; plus a grand total.
- No database access, no formatting, no I/O. This is the entire correctness surface
  and the primary TDD target. Lives in `agent/` alongside `exit_ladder.py`
  (domain logic), not `infra/` (I/O).
- Row-shape contract (documented at the top of the module):
  - entry: `intent_id, channel, ticker, instrument_type, fill_price, fill_qty,
    filled_at` (only `execution_state='filled'` entries are passed in).
  - trim: `intent_id, sold_qty, sold_avg_price, fired_at`.
  - exit: `intent_id, sold_qty, sold_avg_price, created_at`.

### `bin/pnl_report.py` — CLI (mirrors `bin/audit_trader_patterns.py`)
- Sync `sqlite3`, `argparse`, prints a per-source table to stdout.
- Flags:
  - `--db` (default `data/trading_agent.db`)
  - `--channel` (filter to one source; default all)
  - `--since-entry ISO` — include only lots whose entry `filled_at >= date`
  - `--since-sell ISO` — include only realized sells (trims `fired_at` / exits
    `created_at`) on/after date; entries outside the window still appear if they
    have qualifying sells, with realized computed on the in-window sells only
  - default (no `--since-*`) = all-time
  - `--telegram` — also push a compact summary
- The window filters are applied in the **fetch layer** (SQL `WHERE`), so the pure
  core stays window-agnostic. `--since-entry` and `--since-sell` may be combined.
- Exit code 0 always (it's a report, not a check) unless the DB is unreadable.

### Telegram surface = the `--telegram` flag (no new skill/daemon)
- Formats a compact HTML summary (per-source totals + grand total + win rates) and
  sends via the existing `TelegramClient.send_message` (async) wrapped in a one-shot
  `asyncio.run`. The client is built exactly as `main.py` does it:
  `policy = load_policy(--policy)` → `TelegramClient(policy.telegram.bot_token,
  policy.telegram.chat_id)`. `bot_token` resolves `TELEGRAM_BOT_TOKEN` from the env
  via the policy field validator; `chat_id` comes from `config/policy.yaml`. Adds a
  `--policy` flag (default `config/policy.yaml`).
- Rationale: a P&L summary is periodic/on-demand, not a live trade event, so it does
  not belong in the skill chain. One entry point keeps CLI and Telegram math
  identical. The user triggers it manually or via existing scheduling
  (`agent-watchdog`/launchd) — no new cron infrastructure (matches the
  no-red-tape constraint).

## Data flow

1. CLI parses args → builds SQL `WHERE` for the chosen window(s).
2. Fetch filled entries from `trade_intents`, trims from `trade_intent_trims`, exits
   from `position_exits` (each as row dicts; `--since-sell` filters trims/exits,
   `--since-entry` filters entries, `--channel` filters all three).
3. `compute_attribution(...)` aggregates → `AttributionReport`.
4. Format: terminal table to stdout; if `--telegram`, also format + push HTML.

## Error handling

- Missing/unreadable DB → friendly stderr message, non-zero exit.
- Empty result set → print "no realized P&L for the selected window" (and a no-op /
  short Telegram message if `--telegram`), exit 0.
- A `NULL` `fill_price` or `sold_avg_price` on a row → that sell contributes 0 and
  the lot is flagged in output (data-quality marker), never crashes the report.
- Telegram send failure → log to stderr, still print the terminal table, non-zero
  exit only for the send (report already produced).

## Testing

- **Unit** `tests/unit/test_pnl_attribution.py` (the pure core, fixtures):
  full close (gain), full close (loss), partial close, trim-only lot,
  trim + follow-sell mix on one lot, options child lot ($0 realized + open marker),
  multi-source + multi-ticker grouping, win-rate / avg-win / avg-loss / best /
  worst math, **zero-fill exit (`sold_qty=0`) ignored**, **NULL `sold_avg_price`
  with `sold_qty>0` → flagged + excluded**, **`fill_price=0.0` entry → flagged**,
  empty input.
- **Integration** `tests/integration/test_pnl_report_cli.py`:
  seed a temp **file** DB (`tmp_path / "t.db"` via `get_connection`) through the real
  stores (`TradeIntentStore`, `TrimLadderStore`, `PositionExitStore`) — a **file**, not
  `:memory:`, because the sync `sqlite3` CLI opens its own connection and cannot see an
  in-memory aiosqlite DB. Then run the CLI `main()` with `--db` pointed at that file and
  assert table content + `--channel` / `--since-entry` / `--since-sell` filtering. Seed
  must cover the audit findings: an options child lot ($0 realized, included), a
  zero-fill exit (excluded), and a `fill_price=0.0` lot (flagged). `--telegram` path:
  monkeypatch the send (FakeTelegramClient / `mocker.patch`), assert it is called with
  the expected summary; never hits the network.

## Resolved design decisions

- **`--since-sell` semantics**: a lot opened before the window still appears, with
  realized computed on only its in-window sells. (Answers "what did this source make
  me in period X"; a single lot's number can therefore differ between the all-time and
  windowed views — intended.)
- **Win-rate denominator**: a partially-sold lot counts as a closed lot, with realized
  on the sold portion. (Reflects realized cash sooner.)

## Out of scope (YAGNI)

Unrealized / mark-to-market, commissions, an options exit path, scheduled
auto-push / cron wiring, any web UI, cross-lot FIFO matching.
