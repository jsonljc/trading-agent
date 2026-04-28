import textwrap
import tempfile
import os
from pathlib import Path
from agent.policy import load_policy


POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"
with POLICY_PATH.open() as _f:
    BASE_POLICY = _f.read()


def _write(extra_yaml: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(path, "w") as f:
        f.write(BASE_POLICY)
        f.write("\n")
        f.write(extra_yaml)
    return path


def test_discord_extension_config_loads():
    path = _write(textwrap.dedent("""
        discord_extension:
          forwarder_port: 9876
          channel_id_map:
            "111111111111111111": mystic
            "222222222222222222": yonezu
            "333333333333333333": stock-talk-portfolio
    """))
    try:
        policy = load_policy(path)
        assert policy.discord_extension.forwarder_port == 9876
        assert policy.discord_extension.channel_id_map["111111111111111111"] == "mystic"
    finally:
        os.unlink(path)


def test_discord_extension_config_optional():
    """Policy must still parse if the discord_extension block is absent."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(path, "w") as f:
        f.write(BASE_POLICY)
    try:
        policy = load_policy(path)
        assert policy.discord_extension is not None
        assert isinstance(policy.discord_extension.channel_id_map, dict)
    finally:
        os.unlink(path)
