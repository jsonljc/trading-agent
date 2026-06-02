"""Schema guard for the shipped ground-truth fixture files."""
from pathlib import Path

from agent.eval_runner import load_fixtures
from agent.traders.registry import TraderRegistry

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "classifier_eval"
TRADERS_DIR = Path(__file__).resolve().parents[2] / "config" / "traders"

VALID_BUCKETS = {"HIGH", "LOW", "SKIP"}
VALID_SCOPES = {"full", "partial"}


def _real_handles() -> set[str]:
    return {p.handle for p in TraderRegistry.from_dir(TRADERS_DIR).all()}


def test_entry_fixtures_valid():
    fixtures = load_fixtures(FIXTURES_DIR / "entry.jsonl")
    assert len(fixtures) >= 15
    handles = _real_handles()
    seen_buckets = set()
    for fx in fixtures:
        assert fx.kind == "entry"
        assert fx.trader in handles, f"unknown trader handle: {fx.trader}"
        assert fx.expected in VALID_BUCKETS, f"bad bucket: {fx.expected}"
        assert fx.msg.strip()
        seen_buckets.add(fx.expected)
    # all three buckets must be represented
    assert seen_buckets == VALID_BUCKETS


def test_sell_fixtures_valid():
    fixtures = load_fixtures(FIXTURES_DIR / "sell.jsonl")
    assert len(fixtures) >= 10
    handles = _real_handles()
    saw_full = saw_partial = saw_not_sell = False
    for fx in fixtures:
        assert fx.kind == "sell"
        assert fx.trader in handles, f"unknown trader handle: {fx.trader}"
        assert fx.msg.strip()
        exp = fx.expected
        assert isinstance(exp, dict) and "is_sell" in exp and "scope" in exp
        assert isinstance(exp["is_sell"], bool)
        if exp["is_sell"]:
            assert exp["scope"] in VALID_SCOPES, f"bad scope: {exp['scope']}"
            if exp["scope"] == "full":
                saw_full = True
            else:
                saw_partial = True
        else:
            assert exp["scope"] is None
            saw_not_sell = True
    assert saw_full and saw_partial and saw_not_sell
