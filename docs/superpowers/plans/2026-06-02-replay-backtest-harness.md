# Replay / Backtest Harness — Implementation Plan

Date: 2026-06-02
Spec: `docs/superpowers/specs/2026-06-02-replay-backtest-harness-design.md`

Strict TDD: red → green → commit per behavior.

## Task 1 — `ReplayGateway` (agent/replay/gateway.py)
Tests (tests/unit/test_replay_gateway.py):
- qualify_equity / qualify return qualified refs (deterministic).
- get_quote returns default 100.0, and a per-ticker override when supplied.
- get_account_summary returns the configured net_liq.
- get_chain returns [].
- place_order records into placed_orders and returns a trade handle; wait_fill
  returns FILLED for the full qty at the order's limit price.
- cancel_order is a no-op returning True; never raises / never networks.

## Task 2 — `RecordedClassifierClient` (agent/replay/recorded_llm.py)
Tests (tests/unit/test_recorded_llm.py):
- hit: classify returns the recorded response for the message text.
- miss: returns the SKIP default and increments .misses.

## Task 3 — `CapturingTraceStore` (agent/replay/capture.py)
Tests (tests/unit/test_capturing_trace_store.py):
- start/record_skill/finish accumulate the ordered path, updates, terminal status.

## Task 4 — runner (agent/replay/runner.py)
Integration test (tests/integration/test_replay_runner.py):
- Seed a temp FILE db with two signal_events (one clear LONG entry for a real
  configured trader, one SKIP/commentary) + matching classification_log rows with
  llm_response_json. Run replay_all. Assert: entry alert → recorded bucket + a
  would-be BUY with qty>0 and NO real order placed; SKIP alert halts early, no order.

## Task 5 — CLI (bin/replay.py)
Test (tests/integration/test_replay_cli.py):
- Seed a temp file db, run main(), assert table printed + exit 0, and the live db
  is opened read-only (no writes / file mtime + row counts unchanged).

## Task 6 — full suite green + self-review + sample run against real DB.
