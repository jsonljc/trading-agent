#!/usr/bin/env python3
"""Classifier accuracy eval. Read-only; never touches orders or live trading.

Runs the real entry classifier (HIGH/LOW/SKIP) and sell classifier
(is_sell + scope) over the labeled ground-truth fixtures in
tests/fixtures/classifier_eval/ and prints precision/recall/F1, a confusion
matrix, accuracy and macro-F1 — per trader AND pooled.

Determinism: pass --responses FILE (a JSONL cache mapping each message to its
recorded raw LLM response) to run fully offline with no Anthropic call.
--live-llm uses the real AnthropicClassifierClient and is the ONLY path that
makes live calls; it is never exercised by the test suite.

See docs/superpowers/specs/2026-06-02-classifier-accuracy-eval-design.md.

Usage:
    bin/eval_classifiers.py --responses data/eval_responses.jsonl
    bin/eval_classifiers.py --classifier sell --trader mystic --responses ...
    bin/eval_classifiers.py --live-llm            # records/measures real accuracy
    bin/eval_classifiers.py --responses ... --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from agent.classifier_eval import EvalReport, build_report
from agent.eval_runner import Fixture, load_fixtures, run_entry, run_sell
from agent.traders.registry import TraderRegistry
from infra.llm.classifier_client import AnthropicClassifierClient

ENTRY_LABELS = ["HIGH", "LOW", "SKIP"]
IS_SELL_LABELS = ["sell", "not_sell"]
SCOPE_LABELS = ["full", "partial"]


class RecordedLLM:
    """Offline LLM that replays a recorded response keyed on the user message.

    A cache miss raises so a stale/incomplete recording is obvious rather than
    silently scoring wrong.
    """

    def __init__(self, by_msg: dict[str, dict]):
        self._by_msg = by_msg

    async def classify(self, *, system, model, messages) -> dict:
        content = messages[0]["content"]
        if content not in self._by_msg:
            raise KeyError(
                f"no recorded response for message (re-record with --live-llm): "
                f"{content[:80]!r}")
        return self._by_msg[content]


def load_responses(path: str | Path) -> dict[str, dict]:
    by_msg: dict[str, dict] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        by_msg[raw["msg"]] = raw["response"]
    return by_msg


def _build_live_llm():
    """Construct the real Anthropic-backed classifier client (live path only)."""
    import anthropic

    return AnthropicClassifierClient(anthropic.AsyncAnthropic())


async def _eval_entry(fixtures, registry, llm, name) -> EvalReport | None:
    entry_fx = [f for f in fixtures if f.kind == "entry"]
    if not entry_fx:
        return None
    pairs = await run_entry(entry_fx, registry, llm)
    return build_report(name, pairs, ENTRY_LABELS)


async def _eval_sell(fixtures, registry, llm, name) -> tuple[EvalReport, EvalReport] | None:
    sell_fx = [f for f in fixtures if f.kind == "sell"]
    if not sell_fx:
        return None
    is_sell_pairs, scope_pairs = await run_sell(sell_fx, registry, llm)
    is_sell_report = build_report(f"{name} (is_sell)", is_sell_pairs, IS_SELL_LABELS)
    scope_report = build_report(f"{name} (scope)", scope_pairs, SCOPE_LABELS)
    return is_sell_report, scope_report


def render_report(report: EvalReport) -> str:
    lines = []
    lines.append(f"=== {report.classifier}  (n={report.n}, "
                 f"accuracy={report.accuracy * 100:.1f}%, "
                 f"macro-F1={report.macro_f1:.3f}) ===")
    header = f"{'Label':<10} {'Prec':>7} {'Recall':>7} {'F1':>7} " \
             f"{'TP':>4} {'FP':>4} {'FN':>4}"
    lines.append(header)
    lines.append("-" * len(header))
    for label in report.labels:
        m = report.per_class[label]
        lines.append(
            f"{label:<10} {m.precision:>7.3f} {m.recall:>7.3f} {m.f1:>7.3f} "
            f"{m.tp:>4} {m.fp:>4} {m.fn:>4}")
    lines.append("")
    lines.append(_render_confusion(report))
    return "\n".join(lines)


def _render_confusion(report: EvalReport) -> str:
    # The matrix can contain expected/predicted keys outside report.labels — e.g.
    # a real-LLM run predicting "none" (a missed sell). Render those as extra
    # rows/columns so misses are visible in the terminal, not just the JSON.
    labels = list(report.labels)
    extra_pred = {p for row in report.confusion.values() for p in row}
    extra_exp = set(report.confusion)
    cols = labels + sorted((extra_pred | extra_exp) - set(labels))
    rows_order = labels + sorted(extra_exp - set(labels))

    width = max((len(l) for l in cols), default=4) + 1
    width = max(width, 6)
    head = "exp\\pred".ljust(width) + "".join(c.rjust(width) for c in cols)
    rows = [head]
    for exp in rows_order:
        row = report.confusion.get(exp, {})
        cells = "".join(str(row.get(pred, 0)).rjust(width) for pred in cols)
        rows.append(exp.ljust(width) + cells)
    return "\n".join(rows)


async def _gather_reports(fixtures, registry, llm, which, scope_label) -> list[EvalReport]:
    reports: list[EvalReport] = []
    if which in ("entry", "both"):
        r = await _eval_entry(fixtures, registry, llm, f"entry [{scope_label}]")
        if r is not None:
            reports.append(r)
    if which in ("sell", "both"):
        res = await _eval_sell(fixtures, registry, llm, f"sell [{scope_label}]")
        if res is not None:
            reports.extend(res)
    return reports


async def _amain(args, llm) -> int:
    fixtures_dir = Path(args.fixtures_dir)
    fixtures: list[Fixture] = []
    for name in ("entry.jsonl", "sell.jsonl"):
        p = fixtures_dir / name
        if p.exists():
            fixtures.extend(load_fixtures(p))
    if args.trader:
        fixtures = [f for f in fixtures if f.trader == args.trader]
    if not fixtures:
        print(f"error: no fixtures found in {fixtures_dir}", file=sys.stderr)
        return 2

    registry = TraderRegistry.from_dir(args.traders)

    # Per-trader then pooled.
    handles = sorted({f.trader for f in fixtures})
    all_reports: dict[str, list[EvalReport]] = {}
    for handle in handles:
        subset = [f for f in fixtures if f.trader == handle]
        all_reports[handle] = await _gather_reports(
            subset, registry, llm, args.classifier, handle)
    pooled = await _gather_reports(
        fixtures, registry, llm, args.classifier, "pooled")

    if args.json:
        payload = {
            "per_trader": {h: [r.to_dict() for r in rs]
                           for h, rs in all_reports.items()},
            "pooled": [r.to_dict() for r in pooled],
        }
        print(json.dumps(payload, indent=2))
        return 0

    for handle in handles:
        print(f"\n########## TRADER: {handle} ##########")
        for r in all_reports[handle]:
            print(render_report(r))
            print()
    print("\n########## POOLED (all traders) ##########")
    for r in pooled:
        print(render_report(r))
        print()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Classifier accuracy eval")
    parser.add_argument("--fixtures-dir", default="tests/fixtures/classifier_eval")
    parser.add_argument("--traders", default="config/traders")
    parser.add_argument("--classifier", choices=("entry", "sell", "both"),
                        default="both")
    parser.add_argument("--trader", default=None,
                        help="filter fixtures to a single trader handle")
    parser.add_argument("--responses", default=None,
                        help="JSONL cache mapping msg->recorded llm response "
                             "(deterministic, offline)")
    parser.add_argument("--live-llm", action="store_true",
                        help="use the real AnthropicClassifierClient (the only "
                             "path that makes live calls)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.responses:
        llm = RecordedLLM(load_responses(args.responses))
    elif args.live_llm:
        llm = _build_live_llm()
    else:
        print("error: provide --responses FILE for a deterministic run, or "
              "--live-llm to measure real accuracy", file=sys.stderr)
        return 2

    return asyncio.run(_amain(args, llm))


if __name__ == "__main__":
    raise SystemExit(main())
