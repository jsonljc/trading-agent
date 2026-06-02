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

## Authoring note

These messages are **freshly written**, not copied from the traders' own
`conviction_examples` / `sell_examples` in `config/traders/*.yaml`. Copying
those verbatim would leak the few-shot prompt into the eval and inflate
accuracy. Keep new fixtures plausible and consistent with the bucket/scope
definitions documented at the top of each classifier file.

Note: `stocktalkweekly` has `size_floor: HIGH`, so every actionable entry from
that trader is labeled `HIGH` by design.
