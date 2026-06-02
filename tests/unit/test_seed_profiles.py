import pytest
from pathlib import Path
from agent.traders.profile import load_all_profiles, load_profile, VALID_BUCKETS


REPO_ROOT = Path(__file__).resolve().parents[2]
TRADERS_DIR = REPO_ROOT / "config" / "traders"


def test_three_seed_profiles_load():
    profiles = load_all_profiles(TRADERS_DIR)
    handles = {p.handle for p in profiles}
    assert {"wallstengine", "stocktalkweekly", "mystic"}.issubset(handles)


def test_each_seed_profile_has_at_least_three_examples_with_valid_buckets():
    SEED_HANDLES = {"wallstengine", "stocktalkweekly", "mystic"}
    profiles = load_all_profiles(TRADERS_DIR)
    for p in profiles:
        if p.handle not in SEED_HANDLES:
            continue
        assert len(p.conviction_examples) >= 3, f"{p.handle} has too few examples"
        for ex in p.conviction_examples:
            assert ex.bucket in VALID_BUCKETS


def test_stocktalkweekly_has_high_size_floor():
    profiles = load_all_profiles(TRADERS_DIR)
    stw = next(p for p in profiles if p.handle == "stocktalkweekly")
    assert stw.size_floor == "HIGH"


def test_invalid_size_floor_rejected(tmp_path):
    # size_floor only acts on "HIGH"; LOW/SKIP would load as silent no-ops, so
    # they must fail loud at load time (matches the invalid-bucket raise).
    p = tmp_path / "bad.yaml"
    p.write_text(
        "handle: x\ndisplay_name: X\ndiscord_author_pattern: X\n"
        "alert_mention: '@x'\nsize_floor: LOW\n"
    )
    with pytest.raises(ValueError, match="size_floor"):
        load_profile(p)


def test_each_seed_profile_has_sell_examples():
    # The sell classifier ships with per-trader teaching examples (mirrors the
    # entry conviction_examples), not an empty prompt.
    for p in load_all_profiles(TRADERS_DIR):
        assert len(p.sell_examples) >= 2, p.handle
        assert all(e.scope in ("full", "partial") for e in p.sell_examples)


def test_sell_examples_load_and_default_empty(tmp_path):
    from agent.traders.profile import SellExample
    # Defaults to empty when absent.
    p = tmp_path / "noex.yaml"
    p.write_text(
        "handle: x\ndisplay_name: X\ndiscord_author_pattern: X\nalert_mention: '@x'\n")
    assert load_profile(p).sell_examples == ()
    # Loads scope-labelled examples when present.
    p2 = tmp_path / "ex.yaml"
    p2.write_text(
        "handle: y\ndisplay_name: Y\ndiscord_author_pattern: Y\nalert_mention: '@y'\n"
        "sell_examples:\n"
        "  - msg: 'sold half AAPL'\n    scope: partial\n    why: trim\n"
        "  - msg: 'out of NVDA'\n    scope: full\n    why: full exit\n"
    )
    prof = load_profile(p2)
    assert prof.sell_examples == (
        SellExample(msg="sold half AAPL", scope="partial", why="trim"),
        SellExample(msg="out of NVDA", scope="full", why="full exit"),
    )


def test_invalid_sell_example_scope_rejected(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "handle: x\ndisplay_name: X\ndiscord_author_pattern: X\nalert_mention: '@x'\n"
        "sell_examples:\n  - msg: 'sold AAPL'\n    scope: bogus\n")
    with pytest.raises(ValueError, match="scope"):
        load_profile(p)
