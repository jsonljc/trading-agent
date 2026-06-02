# Classifier Accuracy Eval — Plan

Date: 2026-06-02
Spec: docs/superpowers/specs/2026-06-02-classifier-accuracy-eval-design.md

TDD: red → green → commit per behavior. Use
`/Users/jasonli/dev/trading-agent/.venv/bin/python -m pytest`.

## Task 1 — metrics core (`agent/classifier_eval.py`)
- Test `tests/unit/test_classifier_eval.py`: ClassMetrics
  precision/recall/f1 + div-by-zero; confusion_matrix; per_class_metrics
  one-vs-rest (hand-computed); accuracy; macro_f1; empty input;
  EvalReport.to_dict JSON round-trips.
- Implement dataclasses + functions. Commit.

## Task 2 — eval runner (`agent/eval_runner.py`)
- Test `tests/unit/test_eval_runner.py`: Fixture dataclass; load_fixtures
  parses JSONL; run_entry over FakeLLM fixtures (shortcut + LLM path);
  run_sell over FakeLLM fixtures (full / partial / not_sell). Hand-assert
  pairs.
- Implement. Commit.

## Task 3 — fixture set
- Author `tests/fixtures/classifier_eval/entry.jsonl`,
  `sell.jsonl`, `README.md`. Fresh, plausible messages; real handles.
- Test `tests/unit/test_classifier_eval_fixtures.py`: schema guard (fields,
  valid labels, real handles, balanced classes present).
- Commit.

## Task 4 — CLI (`bin/eval_classifiers.py`)
- Test `tests/unit/test_eval_classifiers_cli.py`: main() with --responses
  over a tiny fixtures dir prints metrics, exits 0, no live-llm; --json
  emits valid JSON; missing-llm error path.
- Implement RecordedLLM + CLI. Commit.

## Task 5 — self-review + full suite
- Run full suite green; self-review for YAGNI/quality. Final commit if needed.
