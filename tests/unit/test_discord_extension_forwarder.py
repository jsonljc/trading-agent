from infra.bridge_client.discord_extension_forwarder import map_channel


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
