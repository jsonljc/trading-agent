# Replay / Backtest Harness — Design

Date: 2026-06-02
Branch: `feat/replay-harness`

## Goal
Replay captured historical alerts (`signal_events`) through the LIVE skill chain in
paper mode with NO real orders, to validate classify→gate→size→execute decisions
deterministically. This is an offline correctness / regression tool, not a P&L
backtester.

## Hard constraints
- Paper mode; READ-ONLY on the live DB (`data/trading_agent.db`). The live DB is
  opened read-only and never written. All chain writes go to a fresh in-memory DB
  per alert.
- NO real orders, NO live market-data calls, NO live LLM calls. Determinism via
  fakes: `ReplayGateway`, `RecordedClassifierClient`.
- Reuse existing seams: `build_phase1_chain` / `build_phase2b_execution_chain`
  (agent/registry.py), the `Orchestrator` (agent/orchestrator.py) with its
  on_skip/on_fail/on_success callbacks and the trace_store start/record_skill/finish
  protocol.
- No production-code changes (additive-only if unavoidable). Everything new lives
  under `agent/replay/` + `bin/replay.py` + tests.

## Components

### `agent/replay/gateway.py` — `ReplayGateway`
Deterministic no-op broker implementing every method the chain calls:
- `qualify_equity(ticker)` / `qualify(ref)` → return a qualified `BrokerContractRef`.
- `get_quote(ticker)` → fixed price (default 100.0; per-ticker override map).
- `get_account_summary()` → fixed `AccountSummary` (net_liq default 100_000).
- `get_chain(ticker)` → `[]` so the options sub-chain gracefully no-ops (OptionsChaseGuard
  passes, ChainLookup returns 0 candidates, ContractSelector / OrderSizer / submitter
  terminate as a partial that does not fail the run).
- `place_order(contract, order, client_order_id)` → record into `placed_orders`,
  return a fake trade handle.
- `wait_fill(trade, timeout)` → `FillResult(FILLED)` for the full requested qty at the
  order's limit price.
- `cancel_order(trade)` → no-op True.
Never touches the network or real IB.

### `agent/replay/recorded_llm.py` — `RecordedClassifierClient`
Same interface as `AnthropicClassifierClient.classify(*, system, model, messages)`.
Constructed from `{msg_text: recorded_llm_response_dict}` (parsed from
`classification_log.llm_response_json`). Extracts the user message text from
`messages` and looks it up; on miss returns
`{"is_entry": False, "bucket": "SKIP", "confidence": 0.0}` and increments a miss
counter. Replays the real recorded LLM decision with zero API calls.

### `agent/replay/capture.py` — `CapturingTraceStore`
Implements the trace_store interface; records per trace the ordered
`(skill_name, status)` path and the accumulated `updates`, plus terminal status.

### `agent/replay/runner.py`
- `replay_one(event_row, policy, recorded_llm, *, net_liq, quote) -> ReplayResult`:
  fresh in-memory aiosqlite DB (SCHEMA applied), all needed stores, a `TraderRegistry`
  loaded from config/traders, a no-op telegram client, a `ReplayGateway`. Build phase1
  (entry-only: no exits_store) + phase2b. Swap the `ExecutionEligibilityGuard` for one
  with a fixed RTH `time_fn` (14:30 ET) so execution decisions are deterministic. Build
  Context from the event row exactly like main.py. Run via Orchestrator with a
  `CapturingTraceStore`. Return a `ReplayResult`.
- `replay_all(events, policy, recorded_llm, **kw) -> list[ReplayResult]`.

`ReplayResult` dataclass: event_id, channel, message (truncated), bucket, action_taken,
side, ticker, final_status, final_reason, would_be_orders (summarized placed_orders),
llm_recorded (bool).

### `bin/replay.py` — CLI
Flags: `--db data/trading_agent.db`, `--policy config/policy.yaml`, `--channel`,
`--limit N`, `--event-id ID`, `--net-liq` (100000), `--quote` (100.0), `--json`.
Opens the live DB READ-ONLY, loads filtered `signal_events` + the
`classification_log` recorded LLM responses, runs `replay_all`, prints a per-alert
table + a summary (counts by final_status; how many had no recorded LLM). With
`--json`, prints JSON. Exit 0 normally, 2 if the db is missing. Reports DIVERGENCE:
replayed bucket/action vs recorded `classification_log` bucket/action, flagging
mismatches.

## Determinism choices / notes
- Fixed RTH clock at 14:30 ET so every alert is `EXECUTE_NOW`.
- `get_chain → []` makes options a clean no-op; shares are the validated path.
- The recorded-LLM key is the message text (classification_log.msg_text ==
  ctx.full_message_text). Shortcut-classified alerts (no LLM) are still replayed —
  the deterministic shortcut in TraderClassifier fires before the LLM, so they need
  no recorded response (llm_recorded reflects whether a recorded response existed).
- The live DB's `classification_log.ticker`/`side` columns are NULL in historical
  rows; divergence on ticker/side is therefore derived from the recorded
  llm_response_json where needed, and the primary divergence check is bucket+action.
