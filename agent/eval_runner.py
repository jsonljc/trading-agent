"""Drives the REAL classifiers over labeled fixtures with an injected LLM.

Tests pass a FakeLLM so no live Anthropic call is ever made. The runner sets
only the ctx fields each classifier reads (`trader_handle`,
`full_message_text`, and `bucket` for the sell path) — it deliberately does
not run TraderRouter, because both classifiers consume `trader_handle`
directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.context import Context
from agent.traders.registry import TraderRegistry
from skills.signal.sell_classifier import SellClassifier
from skills.signal.trader_classifier import TraderClassifier


@dataclass
class Fixture:
    msg: str
    trader: str          # trader handle
    kind: str            # "entry" | "sell"
    expected: Any        # entry: bucket str; sell: {"is_sell": bool, "scope": str|None}


def load_fixtures(path: str | Path) -> list[Fixture]:
    """Parse a JSONL fixture file; blank lines tolerated."""
    fixtures: list[Fixture] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        fixtures.append(Fixture(
            msg=raw["msg"],
            trader=raw["trader"],
            kind=raw["kind"],
            expected=raw["expected"],
        ))
    return fixtures


def _ctx(trader_handle: str, msg: str, *, extra: dict | None = None) -> Context:
    data = {"trader_handle": trader_handle, "full_message_text": msg}
    if extra:
        data.update(extra)
    return Context(trace_id="eval", event_id="eval", data=data)


async def run_entry(
    fixtures: list[Fixture], registry: TraderRegistry, llm
) -> list[tuple[str, str]]:
    """Returns (expected_bucket, predicted_bucket) pairs for entry fixtures."""
    classifier = TraderClassifier(registry, llm)
    pairs: list[tuple[str, str]] = []
    for fx in fixtures:
        if fx.kind != "entry":
            continue
        ctx = _ctx(fx.trader, fx.msg)
        await classifier.run(ctx)
        predicted = ctx.get("bucket", "SKIP")
        pairs.append((str(fx.expected), predicted))
    return pairs


async def run_sell(
    fixtures: list[Fixture], registry: TraderRegistry, llm
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Returns (is_sell_pairs, scope_pairs).

    is_sell labels are "sell"/"not_sell". scope pairs are collected only over
    the subset where the fixture's expected.is_sell is True (predicted scope is
    the classifier's sell_scope, or "none" when it did not flag a sell).
    """
    classifier = SellClassifier(registry, llm)
    is_sell_pairs: list[tuple[str, str]] = []
    scope_pairs: list[tuple[str, str]] = []
    for fx in fixtures:
        if fx.kind != "sell":
            continue
        # bucket="SKIP" so the entry-wins guard does not short-circuit the sell.
        ctx = _ctx(fx.trader, fx.msg, extra={"bucket": "SKIP"})
        await classifier.run(ctx)

        predicted_is_sell = "sell" if ctx.get("action") == "sell" else "not_sell"
        expected = fx.expected
        expected_is_sell = "sell" if expected.get("is_sell") else "not_sell"
        is_sell_pairs.append((expected_is_sell, predicted_is_sell))

        if expected.get("is_sell"):
            expected_scope = expected.get("scope") or "none"
            predicted_scope = ctx.get("sell_scope") or "none"
            scope_pairs.append((expected_scope, predicted_scope))
    return is_sell_pairs, scope_pairs
