# Classifier Accuracy Eval — Design

Date: 2026-06-02
Status: design
Branch: feat/classifier-eval

## Goal

A labeled fixture set + metrics (precision / recall / F1, confusion matrix,
accuracy, macro-F1) for BOTH classifiers under test:

- Entry classifier (`skills/signal/trader_classifier.py`): bucket
  classification `{HIGH, LOW, SKIP}` (multi-class).
- Sell classifier (`skills/signal/sell_classifier.py`): sell detection
  evaluated as binary `is_sell` (`sell` / `not_sell`) AND scope
  (`full` / `partial`) over the is_sell-true subset.

The deliverable measures how well the classifiers agree with a curated
ground-truth fixture set. Real accuracy is measured with `--live-llm`; the
committed fixtures + a recorded-responses cache make a regression run fully
deterministic and offline.

## Hard constraints

- NO live LLM / Anthropic API calls in autonomous development or in tests.
  `--live-llm` may exist in the CLI but is never exercised by tests.
- All tests inject a FakeLLM with canned responses (mirrors the FakeLLM
  pattern in `tests/unit/test_trader_classifier.py` /
  `tests/unit/test_sell_classifier.py`).
- Match existing patterns: `bin/` CLIs (`pnl_report.py`,
  `audit_trader_patterns.py`), sync `sqlite3`/`argparse`, dataclasses.
- YAGNI; read-only; paper mode.

## Components

### 1. `agent/classifier_eval.py` — pure metrics core (no I/O, no LLM)

- `@dataclass ClassMetrics`: `tp`, `fp`, `fn` with properties `precision`,
  `recall`, `f1` (each guards div-by-zero → `0.0`).
- `confusion_matrix(pairs) -> dict[expected][predicted] = count`.
- `per_class_metrics(pairs, labels) -> dict[label, ClassMetrics]`
  (one-vs-rest: for label L, tp = expected L & predicted L; fp = predicted L
  & expected != L; fn = expected L & predicted != L).
- `accuracy(pairs) -> float` (fraction expected == predicted; empty → 0.0).
- `macro_f1(metrics) -> float` (mean of per-class f1; empty → 0.0).
- `@dataclass EvalReport`: `classifier` name, `n`, `labels`, `per_class`
  (label → metrics), `confusion`, `accuracy`, `macro_f1`. JSON-serializable
  via `to_dict()`.
- `build_report(name, pairs, labels) -> EvalReport` convenience builder.

### 2. `agent/eval_runner.py` — drives the REAL classifiers over fixtures

- `@dataclass Fixture`: `msg`, `trader` (handle), `kind` (`entry`|`sell`),
  `expected` (entry: bucket str; sell: `{is_sell: bool, scope: str|None}`).
- `load_fixtures(path) -> list[Fixture]` (JSONL).
- `async run_entry(fixtures, registry, llm) -> list[(expected, predicted)]`:
  build a `Context` with `trader_handle` + `full_message_text`, run
  `TraderClassifier(registry, llm).run(ctx)`, predicted =
  `ctx.get("bucket", "SKIP")`.
- `async run_sell(fixtures, registry, llm) -> (is_sell_pairs, scope_pairs)`:
  build a `Context` with `trader_handle` + `full_message_text` +
  `bucket="SKIP"` (so the entry-wins guard does not short-circuit), run
  `SellClassifier(registry, llm).run(ctx)`; predicted is_sell =
  `"sell" if ctx.get("action") == "sell" else "not_sell"`; scope pairs are
  collected only where expected.is_sell is True (predicted scope =
  `ctx.get("sell_scope")` or `"none"` if not flagged).

The runner sets only the ctx fields each classifier actually reads — it does
not run TraderRouter (the classifiers consume `trader_handle` directly).

### 3. Fixture set — `tests/fixtures/classifier_eval/`

- `entry.jsonl` (~15-25 lines spanning HIGH / LOW / SKIP).
- `sell.jsonl` (~10-15 lines spanning full / partial / not_sell).
- `README.md` documenting that these are ground-truth labels; real accuracy
  needs `--live-llm`; fresh fixtures avoid prompt-leakage (no verbatim copies
  of the profiles' own conviction_examples / sell_examples).
- One JSON object per line. `trader` is a real handle from `config/traders`
  (`mystic`, `stocktalkweekly`, `wallstengine`).

### 4. `bin/eval_classifiers.py` — CLI

Flags: `--fixtures-dir` (default `tests/fixtures/classifier_eval`),
`--traders` (default `config/traders`), `--classifier entry|sell|both`
(default `both`), `--trader` (filter), `--responses FILE` (JSONL cache
msg→raw llm response → RecordedLLM), `--live-llm` (real
AnthropicClassifierClient — untested), `--json`.

Behavior: load fixtures, build `TraderRegistry.from_dir`, select LLM
(recorded from `--responses`, else require `--live-llm` with a clear error),
run the eval, print per-class precision/recall/F1 + confusion matrix +
accuracy/macro-F1, per-trader AND pooled. `--json` prints the EvalReport(s)
as JSON. Table style mirrors `bin/pnl_report.py`.

`RecordedLLM`: keyed on the user message text (the `messages[0]["content"]`);
returns the canned dict; raises a clear KeyError-style message on a miss so a
stale cache is obvious.

## Testing (TDD, no live LLM)

- Unit: `ClassMetrics` precision/recall/f1 on known counts; div-by-zero
  guards; `confusion_matrix`; `per_class_metrics` one-vs-rest (hand-computed);
  `accuracy`; `macro_f1`; empty input; `EvalReport.to_dict` JSON-serializable.
- Runner: FakeLLM canned responses over ~4 entry + ~4 sell fixtures; assert
  emitted (expected, predicted) pairs. Includes a deterministic-shortcut entry
  case (no LLM) and an LLM-path case.
- Fixture schema guard: load shipped JSONL, assert required fields + valid
  labels + real handles.
- CLI: run `main()` with `--responses` over a tiny fixtures dir; assert it
  prints metrics and exits 0; assert `--live-llm` path is not triggered.
- Full suite stays green: `.venv/bin/python -m pytest -q`.
