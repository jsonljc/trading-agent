from infra.bridge_client.discord_extension_forwarder import build_envelope, map_channel


CHANNEL_MAP = {
    "111": "mystic",
    "222": "yonezu",
    "333": "stock-talk-portfolio",
}


def test_map_channel_known():
    assert map_channel("111", CHANNEL_MAP) == "mystic"
    assert map_channel("222", CHANNEL_MAP) == "yonezu"


def test_map_channel_unknown_returns_none():
    assert map_channel("999", CHANNEL_MAP) is None


def test_map_channel_empty_id_returns_none():
    assert map_channel("", CHANNEL_MAP) is None
    assert map_channel(None, CHANNEL_MAP) is None


def test_build_envelope_shape():
    env = build_envelope(
        channel="mystic",
        author="Mystic",
        content="OPEN $SHEN — full multi-paragraph thesis goes here ...",
        message_id="987654321098765432",
        received_at="2026-04-28T20:00:00Z",
    )
    assert env["source"] == "discord_ext"
    assert env["channel"] == "mystic"
    assert env["author"] == "Mystic"
    assert env["trigger_preview"].startswith("OPEN $SHEN")
    assert env["received_at"] == "2026-04-28T20:00:00Z"
    # event_id must be deterministic from message_id so retries dedup naturally.
    assert env["event_id"] == "discord_ext:987654321098765432"


def test_build_envelope_received_at_defaults_to_now():
    env = build_envelope(
        channel="mystic", author="Mystic", content="x", message_id="1",
    )
    # Should be ISO-8601 UTC with 'Z' or '+00:00' suffix.
    assert env["received_at"].endswith("Z") or env["received_at"].endswith("+00:00")


def test_build_envelope_preserves_full_content():
    long_body = "a" * 3000
    env = build_envelope(
        channel="mystic", author="Mystic", content=long_body, message_id="1",
    )
    assert env["trigger_preview"] == long_body  # no truncation
