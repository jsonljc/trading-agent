# Classifier accuracy eval — ground-truth fixtures

These JSONL files are **hand-labeled ground truth** for the entry classifier
(`skills/signal/trader_classifier.py`) and the sell classifier
(`skills/signal/sell_classifier.py`). Each line is one message with the label a
human assigned it.

- `entry.jsonl` — entry bucket classification `{HIGH, LOW, SKIP}`.
- `sell.jsonl` — sell detection (`is_sell`) and scope (`full`/`partial`).

## Schema

Entry line:

```json
{"msg": "...", "trader": "wallstengine", "kind": "entry", "expected": "HIGH"}
```

Sell line:

```json
{"msg": "...", "trader": "mystic", "kind": "sell",
 "expected": {"is_sell": true, "scope": "full"}}
```

`trader` is a real handle from `config/traders/` (`mystic`,
`stocktalkweekly`, `wallstengine`).

## How accuracy is measured

Real classifier accuracy requires the live LLM:

```
bin/eval_classifiers.py --live-llm
```

The committed fixtures plus a **recorded-responses cache** (a JSONL mapping
each `msg` to the raw LLM response, supplied via `--responses FILE`) make a
regression run fully **deterministic and offline** — no Anthropic call. All
tests use that recorded/fake path; the `--live-llm` path is never exercised in
tests or autonomous development.

`responses_sample.jsonl` is a committed sample cache (an *ideal* oracle: each
recorded response equals ground truth). It contains **exactly** the messages
that reach the LLM and nothing else — by design it both *excludes* messages
that never call the model and *includes* every message that does:

- **Excluded — entry shortcut:** entries that hit the deterministic stated-size
  shortcut in `trader_classifier.py` (stated `N% pos/position` + an entry verb +
  exactly one ticker) are bucketed without the LLM, so they have no cache entry.
- **Excluded — sell prefilter:** sells with no exit verb are dropped by
  `sell_classifier.py` before any LLM call, so they have no cache entry.
- **Included — everything else:** every other entry and every sell with an exit
  verb (including `is_sell:false` commentary like "sold off"/"dumped") reaches
  the model and therefore *must* be recorded here.

`test_shipped_sample_cache_matches_shipped_fixtures` enforces this invariant: a
missing recording raises `KeyError` and a stale one drops accuracy below 1.0.
It lets you run a deterministic eval immediately:

```
bin/eval_classifiers.py --responses tests/fixtures/classifier_eval/responses_sample.jsonl
```

To measure REAL model accuracy, record the live responses into a committed
cache and replay it:

```
bin/eval_classifiers.py --record tests/fixtures/classifier_eval/recorded_real.jsonl
```

`recorded_real.jsonl` is the committed cache of **actual live-LLM responses**
(distinct from the ideal-oracle `responses_sample.jsonl`): a model mistake shows
up as a wrong prediction, so replaying it measures true accuracy rather than the
plumbing. At the last recording the live classifier scored **100% pooled**
(entry/sell/scope). `test_recorded_real_cache_meets_accuracy_floor` gates it.
Re-record after any prompt/model change to measure current drift; the
`--record` / `--live-llm` paths require `ANTHROPIC_API_KEY` (loaded from `.env`,
or they fail fast with a clear message).

## Authoring note

These messages are **freshly written** with genuinely different sentence
structure from the traders' own `conviction_examples` / `sell_examples` in
`config/traders/*.yaml` — not the same template with a swapped ticker. Those
few-shot examples are injected into the classifier prompt, so a fixture that
merely paraphrases one leaks the prompt into the eval and inflates measured
accuracy via in-context matching. When adding or editing a fixture, read every
example in the relevant trader's config and make sure the new `msg` shares no
sentence shape with any of them. Keep new fixtures plausible and consistent
with the bucket/scope definitions documented at the top of each classifier file.

Note: `stocktalkweekly` has `size_floor: HIGH`, so every actionable entry from
that trader is labeled `HIGH` by design.
