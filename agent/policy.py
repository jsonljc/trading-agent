from __future__ import annotations
import os
import yaml
from pydantic import BaseModel, field_validator


class TriggerPolicy(BaseModel):
    action_words: list[str]


class InstrumentPolicy(BaseModel):
    prefer_options: bool
    min_expiry_days: int
    strike_policy: str
    fallback_to_stock_if_no_options: bool


class PricingPolicy(BaseModel):
    mode: str
    option_spread_fraction: float
    stock_buffer_pct: float


class SizingPolicy(BaseModel):
    low_conviction_pct: float
    high_conviction_pct: float


class MarketHours(BaseModel):
    options_rth_only: bool
    stock_premarket_allowed: bool
    stock_premarket_start: str
    rth_start: str
    rth_end: str
    stock_afterhours_queue: bool


class CooldownPolicy(BaseModel):
    enabled: bool


class DedupePolicy(BaseModel):
    enabled: bool
    key: str


class PricingGuards(BaseModel):
    min_bid: float
    max_spread_pct: float


class ModelsConfig(BaseModel):
    vision: str
    text: str


class TelegramConfig(BaseModel):
    chat_id: str
    bot_token: str

    @field_validator("bot_token", mode="before")
    @classmethod
    def resolve_bot_token(cls, v: str) -> str:
        return os.environ.get("TELEGRAM_BOT_TOKEN", v)


class PolicyModel(BaseModel):
    trigger: TriggerPolicy
    instrument_policy: InstrumentPolicy
    pricing_policy: PricingPolicy
    sizing_policy: SizingPolicy
    market_hours: MarketHours
    cooldown_policy: CooldownPolicy
    dedupe_policy: DedupePolicy
    pricing_policy_guards: PricingGuards
    models: ModelsConfig
    watched_channels: list[str]
    discord_bundle_id: str
    telegram: TelegramConfig


def load_policy(path: str) -> PolicyModel:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return PolicyModel.model_validate(raw)
