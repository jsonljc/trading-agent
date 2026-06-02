from __future__ import annotations
import os
import yaml
from pydantic import BaseModel, Field, field_validator


class TriggerPolicy(BaseModel):
    action_words: list[str]


class InstrumentPolicy(BaseModel):
    min_expiry_days: int
    strike_policy: str
    # prefer_options removed (dead under shares-first design)
    # fallback_to_stock_if_no_options removed (dead under shares-first design)


class MarketHours(BaseModel):
    options_rth_only: bool
    stock_premarket_allowed: bool
    stock_premarket_start: str
    rth_start: str
    rth_end: str
    stock_afterhours_queue: bool


class ChannelConfig(BaseModel):
    # auto_execute moved to trader profile (single source of truth). The
    # watched_channels dict now serves only as a tracked-channel registry.
    model_config = {"extra": "ignore"}


class CooldownPolicy(BaseModel):
    enabled: bool
    cooldown_minutes: int = 30


class DedupePolicy(BaseModel):
    enabled: bool
    key: str


class PricingGuards(BaseModel):
    min_bid: float
    max_spread_pct: float            # (ask-bid)/mid ceiling for the options leg
    min_open_interest: int = 100     # reject when OI is present and below this
    min_volume: int = 0              # same-day volume floor (0 = off until live data)


class ModelsConfig(BaseModel):
    vision: str
    text: str


class TelegramConfig(BaseModel):
    chat_id: str
    bot_token: str

    @field_validator("bot_token", mode="before")
    @classmethod
    def resolve_bot_token(cls, v: str) -> str:
        return os.environ.get("TELEGRAM_BOT_TOKEN") or v


class IBGatewayPolicy(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    mode: str = "paper"
    paper_account_prefixes: list[str] = ["DU"]

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v


class SizingTier(BaseModel):
    shares: float = Field(ge=0.0, le=1.0)
    options: float = Field(ge=0.0, le=1.0)


class SizingBuckets(BaseModel):
    high: SizingTier
    low: SizingTier


class SizingPolicy(BaseModel):
    default: SizingBuckets
    per_channel: dict[str, SizingBuckets] = Field(default_factory=dict)


class TrimRung(BaseModel):
    threshold_pct: float = Field(ge=0.0, le=1.0)
    trim_pct: float = Field(ge=0.0, le=1.0)


class TrimLadderConfig(BaseModel):
    rungs: list[TrimRung]


class ExecutionPolicy(BaseModel):
    fill_wait_timeout_seconds: float = 30.0
    max_equity_price: float = 500.0
    reconciler_interval_seconds: int = 60
    # walk_profile, walk_profiles, reprice_interval_ms, max_chase_pct removed
    # (dead — PriceWalker bypassed by MKT chain)
    margin_multiplier: float = 2.0
    options_chase_threshold_pct: float = 0.10
    # Marketable-limit slippage caps: limit = live_ask * (1 + cap). Options
    # spreads are wide, so the options cap is looser than the shares cap.
    # Bounded to [0, 0.5): a misconfigured cap >= 1 would yield a non-positive
    # sell limit / runaway buy limit.
    options_slippage_cap_pct: float = Field(default=0.05, ge=0.0, lt=0.5)
    shares_slippage_cap_pct: float = Field(default=0.01, ge=0.0, lt=0.5)
    # Operator emergency stop: `touch` this file to halt NEW entries instantly.
    kill_switch_file: str = "data/KILL"
    exit_poll_interval_seconds: int = 2
    trim_ladder: TrimLadderConfig = TrimLadderConfig(rungs=[
        TrimRung(threshold_pct=0.05, trim_pct=0.40),
        TrimRung(threshold_pct=0.10, trim_pct=0.40),
    ])
    sizing: SizingPolicy = SizingPolicy(
        default=SizingBuckets(
            high=SizingTier(shares=0.10, options=0.05),
            low=SizingTier(shares=0.05, options=0.05),
        ),
    )


class DiscordExtensionConfig(BaseModel):
    forwarder_port: int = 9876
    channel_id_map: dict[str, str] = Field(default_factory=dict)


class PolicyModel(BaseModel):
    trigger: TriggerPolicy
    instrument_policy: InstrumentPolicy
    # pricing_policy removed (dead — only consumed by OrderPricer, which is removed)
    market_hours: MarketHours
    cooldown_policy: CooldownPolicy
    dedupe_policy: DedupePolicy
    pricing_policy_guards: PricingGuards
    models: ModelsConfig
    watched_channels: dict[str, ChannelConfig]
    discord_bundle_id: str
    telegram: TelegramConfig
    ib_gateway: IBGatewayPolicy = IBGatewayPolicy()
    execution: ExecutionPolicy = ExecutionPolicy()
    discord_extension: DiscordExtensionConfig = DiscordExtensionConfig()


def load_policy(path: str) -> PolicyModel:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return PolicyModel.model_validate(raw)
