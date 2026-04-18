import pytest
from pydantic import ValidationError
from agent.policy import PolicyModel, load_policy


def test_load_policy_from_valid_yaml(tmp_path):
    yaml_content = """
trigger:
  action_words: ["long", "adding"]
instrument_policy:
  prefer_options: true
  min_expiry_days: 180
  strike_policy: closest_itm_call
  fallback_to_stock_if_no_options: true
pricing_policy:
  mode: cheapest_fillable_limit
  option_spread_fraction: 0.25
  stock_buffer_pct: 0.001
sizing_policy:
  low_conviction_pct: 0.05
  high_conviction_pct: 0.10
market_hours:
  options_rth_only: true
  stock_premarket_allowed: true
  stock_premarket_start: "04:00"
  rth_start: "09:30"
  rth_end: "16:00"
  stock_afterhours_queue: true
cooldown_policy:
  enabled: false
dedupe_policy:
  enabled: true
  key: message_fingerprint_plus_ticker_plus_action_plus_window
pricing_policy_guards:
  min_bid: 0.01
  max_spread_pct: 0.40
models:
  vision: claude-opus-4-7
  text: claude-haiku-4-5-20251001
watched_channels: ["mystic"]
discord_bundle_id: "com.hnc.Discord"
telegram:
  chat_id: "123"
  bot_token: "abc"
"""
    f = tmp_path / "policy.yaml"
    f.write_text(yaml_content)
    policy = load_policy(str(f))
    assert policy.sizing_policy.low_conviction_pct == 0.05
    assert policy.market_hours.rth_start == "09:30"
    assert policy.telegram.chat_id == "123"


def test_load_policy_fails_on_missing_key(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("trigger:\n  action_words: []\n")
    with pytest.raises(ValidationError):
        load_policy(str(f))


def test_load_policy_bot_token_env_override(tmp_path, monkeypatch):
    yaml_content = """
trigger:
  action_words: ["long"]
instrument_policy:
  prefer_options: true
  min_expiry_days: 180
  strike_policy: closest_itm_call
  fallback_to_stock_if_no_options: true
pricing_policy:
  mode: cheapest_fillable_limit
  option_spread_fraction: 0.25
  stock_buffer_pct: 0.001
sizing_policy:
  low_conviction_pct: 0.05
  high_conviction_pct: 0.10
market_hours:
  options_rth_only: true
  stock_premarket_allowed: true
  stock_premarket_start: "04:00"
  rth_start: "09:30"
  rth_end: "16:00"
  stock_afterhours_queue: true
cooldown_policy:
  enabled: false
dedupe_policy:
  enabled: true
  key: message_fingerprint_plus_ticker_plus_action_plus_window
pricing_policy_guards:
  min_bid: 0.01
  max_spread_pct: 0.40
models:
  vision: claude-opus-4-7
  text: claude-haiku-4-5-20251001
watched_channels: ["mystic"]
discord_bundle_id: "com.hnc.Discord"
telegram:
  chat_id: "123"
  bot_token: "yaml_token"
"""
    f = tmp_path / "policy.yaml"
    f.write_text(yaml_content)

    # Without env var: uses YAML value
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    policy = load_policy(str(f))
    assert policy.telegram.bot_token == "yaml_token"

    # With env var: env var overrides YAML
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env_token")
    policy2 = load_policy(str(f))
    assert policy2.telegram.bot_token == "env_token"
