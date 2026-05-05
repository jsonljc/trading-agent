import pytest
from agent.policy import load_policy


def test_live_policy_yaml_loads():
    pol = load_policy("config/policy.yaml")
    cm = pol.discord_extension.channel_id_map
    assert cm["1229546005788098580"] == "stocktalkweekly"
    assert cm["1217309136681832540"] == "mystic"
    assert cm["1248378121451733083"] == "wallstengine"
    assert cm["1221605346305642558"] == "pup-danny"
    assert cm["1151611275709788253"] == "urkel"
    s = pol.execution.sizing
    assert s.per_channel["stock-talk-portfolio"].high.shares == 0.20
    assert s.per_channel["mystic"].low.shares == 0.10
    assert s.default.high.shares == 0.10
    assert pol.execution.margin_multiplier == 2.0
    assert pol.execution.options_chase_threshold_pct == 0.10
