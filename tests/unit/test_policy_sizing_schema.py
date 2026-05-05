import pytest
from agent.policy import PolicyModel


def _base_policy_dict() -> dict:
    return {
        "trigger": {"action_words": ["long"]},
        "instrument_policy": {
            "min_expiry_days": 180,
            "strike_policy": "closest_itm_call",
        },
        "market_hours": {"options_rth_only": True, "stock_premarket_allowed": True,
                         "stock_premarket_start": "04:00", "rth_start": "09:30",
                         "rth_end": "16:00", "stock_afterhours_queue": True},
        "cooldown_policy": {"enabled": True, "cooldown_minutes": 30},
        "dedupe_policy": {"enabled": True, "key": "x"},
        "pricing_policy_guards": {"min_bid": 0.01, "max_spread_pct": 0.40},
        "models": {"vision": "claude-opus-4-7", "text": "claude-haiku-4-5"},
        "watched_channels": {"mystic": {"auto_execute": True}},
        "discord_bundle_id": "x",
        "telegram": {"chat_id": "1", "bot_token": "x"},
        "execution": {
            "margin_multiplier": 2.0,
            "options_chase_threshold_pct": 0.10,
            "exit_poll_interval_seconds": 2,
            "trim_ladder": {"rungs": [
                {"threshold_pct": 0.05, "trim_pct": 0.40},
                {"threshold_pct": 0.10, "trim_pct": 0.40},
            ]},
            "sizing": {
                "default": {
                    "high": {"shares": 0.10, "options": 0.05},
                    "low":  {"shares": 0.05, "options": 0.05},
                },
                "per_channel": {
                    "stock-talk-portfolio": {
                        "high": {"shares": 0.20, "options": 0.05},
                        "low":  {"shares": 0.15, "options": 0.05},
                    },
                    "mystic": {
                        "high": {"shares": 0.15, "options": 0.05},
                        "low":  {"shares": 0.10, "options": 0.05},
                    },
                },
            },
        },
    }


def test_loads_full_sizing_table():
    pol = PolicyModel.model_validate(_base_policy_dict())
    assert pol.execution.margin_multiplier == 2.0
    assert pol.execution.options_chase_threshold_pct == 0.10
    assert pol.execution.exit_poll_interval_seconds == 2
    assert pol.execution.trim_ladder.rungs[0].threshold_pct == 0.05
    assert pol.execution.sizing.default.high.shares == 0.10
    assert pol.execution.sizing.per_channel["stock-talk-portfolio"].high.shares == 0.20


def test_rejects_out_of_range_pct():
    bad = _base_policy_dict()
    bad["execution"]["sizing"]["default"]["high"]["shares"] = 2.0
    with pytest.raises(Exception):
        PolicyModel.model_validate(bad)


def test_default_margin_multiplier_when_omitted():
    d = _base_policy_dict()
    d["execution"].pop("margin_multiplier")
    pol = PolicyModel.model_validate(d)
    assert pol.execution.margin_multiplier == 2.0
