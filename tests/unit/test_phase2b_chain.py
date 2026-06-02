import pytest
from unittest.mock import MagicMock
from agent.policy import (PolicyModel, ExecutionPolicy, SizingPolicy, SizingBuckets,
                          SizingTier, TrimLadderConfig, TrimRung)


def _build_test_policy() -> PolicyModel:
    """Minimal policy that satisfies the Pydantic schema for chain building."""
    import yaml
    return PolicyModel.model_validate(yaml.safe_load("""
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
  key: x
pricing_policy_guards:
  min_bid: 0.01
  max_spread_pct: 0.40
models:
  vision: claude-opus-4-7
  text: claude-haiku-4-5
watched_channels:
  mystic:
    auto_execute: true
discord_bundle_id: "x"
telegram:
  chat_id: "1"
  bot_token: "x"
"""))


def test_chain_has_expected_skill_order():
    from agent.registry import build_phase2b_execution_chain

    policy = _build_test_policy()
    execution_store = MagicMock()
    execution_store._conn = MagicMock()
    intent_store = MagicMock()
    trim_store = MagicMock()
    gateway = MagicMock()

    chain = build_phase2b_execution_chain(
        policy=policy,
        execution_store=execution_store,
        gateway=gateway,
        trade_intent_store=intent_store,
        trim_store=trim_store,
    )
    names = [s.name for s in chain]
    assert names == [
        "KillSwitchGuard",
        "TradeIntentWriter",
        "ChannelPolicyGuard",
        "CooldownGuard",
        "ExecutionEligibilityGuard",
        "RthEntryGuard",
        "ReferencePriceCapture",
        "SizingResolver",
        "EquityContractBuilder",
        "OrderSizer",
        "SharesMarketSubmitter",
        "OptionsChaseGuard",
        "ChainLookup",
        "InstrumentMarketabilityGuard",
        "ContractSelector",
        "OrderSizer",
        "OptionsMarketSubmitter",
    ]


def test_chain_works_without_intent_store():
    """When intent_store is None, the leading 3 guards are skipped."""
    from agent.registry import build_phase2b_execution_chain
    policy = _build_test_policy()
    execution_store = MagicMock()
    execution_store._conn = MagicMock()
    chain = build_phase2b_execution_chain(
        policy=policy, execution_store=execution_store, gateway=MagicMock(),
        trade_intent_store=None, trim_store=None,
    )
    names = [s.name for s in chain]
    # No TradeIntentWriter / ChannelPolicyGuard / CooldownGuard at the start
    assert "TradeIntentWriter" not in names
    # KillSwitchGuard still leads (it needs no intent store).
    assert names[0] == "KillSwitchGuard"
    assert names[1] == "ExecutionEligibilityGuard"
