"""Per-channel liveness recording in the Discord extension forwarder.

These cover the forwarder side of the silent-capture-death fix: the extension
emits a periodic per-channel beacon (and every captured signal stamps its
channel); the forwarder persists the most-recent timestamp per TRACKED channel
to a self-describing JSON file that bin/agent-watchdog reads from a separate
process.
"""
import json

from infra.bridge_client.discord_extension_forwarder import (
    ChannelLivenessStore,
    beacon_channel_ids,
)

CHANNEL_MAP = {
    "111": "mystic",
    "222": "wallstengine",
    "333": "stocktalkweekly",
}


def test_store_seeds_all_tracked_channels(tmp_path):
    p = tmp_path / "liveness.json"
    store = ChannelLivenessStore(CHANNEL_MAP, path=p)
    store.seed("2026-06-26T13:00:00Z")
    data = json.loads(p.read_text())
    # Self-describing: watchdog needs no policy access to learn the roster.
    assert set(data["tracked"]) == {"mystic", "wallstengine", "stocktalkweekly"}
    # Cold-start: seed every tracked channel so the watchdog doesn't fire during
    # the first beacon interval, but a never-beaconing channel still goes stale.
    assert set(data["channels"]) == {"mystic", "wallstengine", "stocktalkweekly"}
    assert data["channels"]["mystic"] == "2026-06-26T13:00:00Z"
    assert "updated_at" in data


def test_record_ids_maps_and_stamps(tmp_path):
    p = tmp_path / "liveness.json"
    store = ChannelLivenessStore(CHANNEL_MAP, path=p)
    store.seed("2026-06-26T13:00:00Z")
    store.record_ids(["111"], "2026-06-26T13:05:00Z")
    data = json.loads(p.read_text())
    assert data["channels"]["mystic"] == "2026-06-26T13:05:00Z"
    # Untouched channels keep their seed time.
    assert data["channels"]["wallstengine"] == "2026-06-26T13:00:00Z"


def test_record_ids_handles_multiple(tmp_path):
    p = tmp_path / "liveness.json"
    store = ChannelLivenessStore(CHANNEL_MAP, path=p)
    store.seed("2026-06-26T13:00:00Z")
    store.record_ids(["111", "222"], "2026-06-26T13:07:00Z")
    data = json.loads(p.read_text())
    assert data["channels"]["mystic"] == "2026-06-26T13:07:00Z"
    assert data["channels"]["wallstengine"] == "2026-06-26T13:07:00Z"
    assert data["channels"]["stocktalkweekly"] == "2026-06-26T13:00:00Z"


def test_record_ids_ignores_unmapped(tmp_path):
    p = tmp_path / "liveness.json"
    store = ChannelLivenessStore(CHANNEL_MAP, path=p)
    store.seed("2026-06-26T13:00:00Z")
    store.record_ids(["999"], "2026-06-26T13:05:00Z")  # not a tracked channel
    data = json.loads(p.read_text())
    assert "999" not in json.dumps(data)
    assert data["channels"]["mystic"] == "2026-06-26T13:00:00Z"


def test_record_ids_empty_is_noop(tmp_path):
    p = tmp_path / "liveness.json"
    store = ChannelLivenessStore(CHANNEL_MAP, path=p)
    store.seed("2026-06-26T13:00:00Z")
    store.record_ids([], "2026-06-26T13:05:00Z")
    store.record_ids(None, "2026-06-26T13:05:00Z")
    data = json.loads(p.read_text())
    assert data["channels"]["mystic"] == "2026-06-26T13:00:00Z"


def test_write_is_atomic_no_leftover_tmp(tmp_path):
    p = tmp_path / "liveness.json"
    store = ChannelLivenessStore(CHANNEL_MAP, path=p)
    store.seed("2026-06-26T13:00:00Z")
    store.record_ids(["111"], "2026-06-26T13:05:00Z")
    assert list(tmp_path.glob("*.tmp")) == []
    json.loads(p.read_text())  # valid JSON


def test_store_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "dir" / "liveness.json"
    store = ChannelLivenessStore(CHANNEL_MAP, path=p)
    store.seed("2026-06-26T13:00:00Z")
    assert p.exists()


# --- beacon payload parsing -------------------------------------------------

def test_beacon_channel_ids_channels_list():
    assert beacon_channel_ids({"channels": ["111", "222"]}) == ["111", "222"]


def test_beacon_channel_ids_watching_key():
    assert beacon_channel_ids({"watching": ["111"]}) == ["111"]


def test_beacon_channel_ids_singular_channel_id():
    assert beacon_channel_ids({"channel_id": "111"}) == ["111"]


def test_beacon_channel_ids_empty():
    assert beacon_channel_ids({}) == []
    assert beacon_channel_ids({"channels": []}) == []


def test_beacon_channel_ids_coerces_to_str():
    assert beacon_channel_ids({"channels": [111, 222]}) == ["111", "222"]


def test_beacon_channel_ids_ignores_non_dict():
    assert beacon_channel_ids(None) == []
    assert beacon_channel_ids("nope") == []
