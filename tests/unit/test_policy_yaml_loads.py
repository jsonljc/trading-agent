import pytest
from agent.policy import load_policy


def test_live_policy_yaml_loads():
    pol = load_policy("config/policy.yaml")
    cm = pol.discord_extension.channel_id_map
    assert cm["1229546005788098580"] == "stocktalkweekly"
    assert cm["1217309136681832540"] == "mystic"
    assert cm["1248378121451733083"] == "wallstengine"
    # pup-danny and urkel were removed 2026-05-12 — too noisy / recap-heavy
    s = pol.execution.sizing
    assert s.per_channel["stocktalkweekly"].high.shares == 0.20
    assert s.per_channel["mystic"].low.shares == 0.10
    # watched_channels keys must match the discord extension's channel_id_map values
    # (single-word handles), otherwise ChannelPolicyGuard silently blocks.
    assert set(pol.watched_channels.keys()) >= set(cm.values())
    assert s.default.high.shares == 0.10
    assert pol.execution.margin_multiplier == 2.0
    assert pol.execution.options_chase_threshold_pct == 0.10
