import pytest
from pydantic import ValidationError
from agent.policy import PolicyModel, load_policy


def test_load_policy_from_valid_yaml(tmp_path):
    yaml_content = """
trigger:
  action_words: ["long", "adding"]
instrument_policy:
  min_expiry_days: 180
  strike_policy: closest_itm_call
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
watched_channels:
  mystic:
    auto_execute: true
discord_bundle_id: "com.hnc.Discord"
telegram:
  chat_id: "123"
  bot_token: "abc"
"""
    f = tmp_path / "policy.yaml"
    f.write_text(yaml_content)
    policy = load_policy(str(f))
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
  min_expiry_days: 180
  strike_policy: closest_itm_call
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
watched_channels:
  mystic:
    auto_execute: true
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


def test_channel_config_auto_execute():
    raw = """
trigger:
  action_words: ["long"]
instrument_policy:
  min_expiry_days: 180
  strike_policy: closest_itm_call
market_hours:
  options_rth_only: true
  stock_premarket_allowed: true
  stock_premarket_start: "04:00"
  rth_start: "09:30"
  rth_end: "16:00"
  stock_afterhours_queue: true
cooldown_policy:
  enabled: true
  cooldown_minutes: 30
dedupe_policy:
  enabled: true
  key: message_fingerprint_plus_ticker_plus_action_plus_window
pricing_policy_guards:
  min_bid: 0.01
  max_spread_pct: 0.40
models:
  vision: claude-opus-4-7
  text: claude-haiku-4-5-20251001
watched_channels:
  mystic:
    auto_execute: true
  chat:
    auto_execute: false
discord_bundle_id: "com.hnc.Discord"
telegram:
  chat_id: "123"
  bot_token: "fake"
"""
    import yaml
    from agent.policy import PolicyModel
    policy = PolicyModel.model_validate(yaml.safe_load(raw))
    assert policy.watched_channels["mystic"].auto_execute is True
    assert policy.watched_channels["chat"].auto_execute is False
    assert policy.cooldown_policy.cooldown_minutes == 30


