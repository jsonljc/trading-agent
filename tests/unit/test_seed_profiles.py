from pathlib import Path
from agent.traders.profile import load_all_profiles, VALID_BUCKETS


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
