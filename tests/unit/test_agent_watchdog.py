"""Unit tests for bin/agent-watchdog.

The script has no .py extension and a hyphen in its name, so it is loaded from
its file path via importlib. Module-level code only sets up logging + constants
(main() is guarded by __main__), so importing is side-effect-safe.

Covers the silent-capture-death fix:
  * active window now spans premarket -> close (not just RTH);
  * per-channel staleness detection from the forwarder's liveness file.
"""
import importlib.machinery
import importlib.util
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")


def _load_watchdog():
    # bin/agent-watchdog has no .py extension, so use an explicit source loader.
    loader = importlib.machinery.SourceFileLoader(
        "agent_watchdog_under_test", str(REPO / "bin" / "agent-watchdog")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


wd = _load_watchdog()


# --- active window (premarket-inclusive) ------------------------------------

OPEN = time(4, 0)
CLOSE = time(16, 0)


def test_premarket_is_active():
    dt = datetime(2026, 6, 26, 5, 0, tzinfo=ET)  # 05:00 ET Friday
    assert wd.in_active_window(dt, OPEN, CLOSE) is True


def test_before_premarket_is_inactive():
    dt = datetime(2026, 6, 26, 3, 30, tzinfo=ET)
    assert wd.in_active_window(dt, OPEN, CLOSE) is False


def test_rth_is_active():
    dt = datetime(2026, 6, 26, 10, 0, tzinfo=ET)
    assert wd.in_active_window(dt, OPEN, CLOSE) is True


def test_after_close_is_inactive():
    dt = datetime(2026, 6, 26, 16, 30, tzinfo=ET)
    assert wd.in_active_window(dt, OPEN, CLOSE) is False


def test_weekend_is_inactive_even_in_window():
    dt = datetime(2026, 6, 27, 10, 0, tzinfo=ET)  # Saturday
    assert wd.in_active_window(dt, OPEN, CLOSE) is False


def test_premarket_open_boundary_inclusive():
    assert wd.in_active_window(datetime(2026, 6, 26, 4, 0, tzinfo=ET), OPEN, CLOSE) is True


def test_close_boundary_inclusive():
    assert wd.in_active_window(datetime(2026, 6, 26, 16, 0, tzinfo=ET), OPEN, CLOSE) is True


# --- premarket bound resolution from policy ---------------------------------

def test_policy_premarket_open_reads_value(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text('market_hours:\n  stock_premarket_start: "07:30"\n  rth_start: "09:30"\n')
    assert wd._policy_premarket_open(p) == time(7, 30)


def test_policy_premarket_open_fallback_missing_key(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text('market_hours:\n  rth_start: "09:30"\n')
    assert wd._policy_premarket_open(p) == time(4, 0)


def test_policy_premarket_open_fallback_no_file(tmp_path):
    assert wd._policy_premarket_open(tmp_path / "nope.yaml") == time(4, 0)


def test_policy_premarket_open_live_policy_is_4am():
    # The live config/policy.yaml allows premarket from 04:00.
    assert wd._policy_premarket_open() == time(4, 0)


# --- per-channel staleness --------------------------------------------------

def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_stale_channels_all_fresh():
    now = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    fresh = _iso(now - timedelta(minutes=2))
    liveness = {"tracked": ["mystic", "wse"], "channels": {"mystic": fresh, "wse": fresh}}
    assert wd.stale_channels(liveness, now, 15) == []


def test_stale_channels_one_old():
    now = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    fresh = _iso(now - timedelta(minutes=2))
    old = _iso(now - timedelta(minutes=40))
    liveness = {"tracked": ["mystic", "wse"], "channels": {"mystic": fresh, "wse": old}}
    assert wd.stale_channels(liveness, now, 15) == ["wse"]


def test_stale_channels_missing_entry_is_stale():
    now = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    fresh = _iso(now - timedelta(minutes=2))
    liveness = {"tracked": ["mystic", "wse"], "channels": {"mystic": fresh}}
    assert wd.stale_channels(liveness, now, 15) == ["wse"]


def test_stale_channels_bad_timestamp_is_stale():
    now = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    liveness = {"tracked": ["mystic"], "channels": {"mystic": "not-a-date"}}
    assert wd.stale_channels(liveness, now, 15) == ["mystic"]


def test_stale_channels_empty_tracked():
    now = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    assert wd.stale_channels({"tracked": [], "channels": {}}, now, 15) == []


def test_stale_channels_accepts_et_aware_now():
    # main() passes an ET-aware `now`; comparison to UTC stamps must still work.
    now_et = datetime(2026, 6, 26, 10, 0, tzinfo=ET)
    fresh = _iso(now_et.astimezone(timezone.utc) - timedelta(minutes=1))
    liveness = {"tracked": ["mystic"], "channels": {"mystic": fresh}}
    assert wd.stale_channels(liveness, now_et, 15) == []


def test_stale_channels_boundary_at_threshold():
    now = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    just_inside = _iso(now - timedelta(minutes=14, seconds=59))
    just_outside = _iso(now - timedelta(minutes=15, seconds=1))
    assert wd.stale_channels({"tracked": ["a"], "channels": {"a": just_inside}}, now, 15) == []
    assert wd.stale_channels({"tracked": ["a"], "channels": {"a": just_outside}}, now, 15) == ["a"]


# --- liveness file parsing/reading ------------------------------------------

def test_parse_liveness_bad_json_returns_none():
    assert wd.parse_liveness("{not json") is None


def test_parse_liveness_non_dict_returns_none():
    assert wd.parse_liveness("[1, 2, 3]") is None


def test_parse_liveness_ok():
    assert wd.parse_liveness('{"tracked": []}') == {"tracked": []}


def test_read_liveness_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(wd, "LIVENESS_PATH", tmp_path / "nope.json")
    assert wd.read_liveness() is None


def test_read_liveness_ok(monkeypatch, tmp_path):
    p = tmp_path / "liveness.json"
    p.write_text('{"tracked": ["mystic"], "channels": {}}')
    monkeypatch.setattr(wd, "LIVENESS_PATH", p)
    assert wd.read_liveness()["tracked"] == ["mystic"]
