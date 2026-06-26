import pytest
import yaml
from unittest.mock import AsyncMock
from agent.context import Context
from agent.policy import PolicyModel
from infra.ib.models import AccountSummary
from infra.ib.gateway import IBGatewayUnavailable
from skills.risk.exposure_guard import ExposureGuard


def _policy(max_equity_price=500.0, max_deployed_pct=1.0):
    return PolicyModel.model_validate(yaml.safe_load(f"""
trigger: {{action_words: ["long"]}}
instrument_policy: {{min_expiry_days: 180, strike_policy: closest_itm_call}}
market_hours: {{options_rth_only: true, stock_premarket_allowed: true,
  stock_premarket_start: "04:00", rth_start: "09:30", rth_end: "16:00",
  stock_afterhours_queue: true}}
cooldown_policy: {{enabled: true, cooldown_minutes: 30}}
dedupe_policy: {{enabled: true, key: x}}
pricing_policy_guards: {{min_bid: 0.01, max_spread_pct: 0.4}}
models: {{vision: v, text: t}}
watched_channels: {{mystic: {{}}}}
discord_bundle_id: x
telegram: {{chat_id: "1", bot_token: x}}
execution: {{max_equity_price: {max_equity_price}, max_deployed_pct: {max_deployed_pct}}}
"""))


class FakeGateway:
    def __init__(self, net_liq=100_000.0, buying_power=200_000.0, raise_=False):
        self._net_liq = net_liq
        self._bp = buying_power
        self._raise = raise_

    async def get_account_summary(self):
        if self._raise:
            raise IBGatewayUnavailable("down")
        return AccountSummary(buying_power=self._bp, net_liquidation=self._net_liq,
                              currency="USD")


def _ctx(ref=100.0):
    return Context(trace_id="t", event_id="e",
                   data={"reference_price": ref, "ticker": "X"})


@pytest.mark.asyncio
async def test_above_max_equity_price_skips():
    guard = ExposureGuard(_policy(max_equity_price=500.0), FakeGateway(),
                          trade_intent_store=object(),
                          exposure_fn=AsyncMock(return_value=0.0))
    result = await guard.run(_ctx(ref=750.0))
    assert result.status == "skip"
    assert "above_max_equity_price" in (result.reason or "")


@pytest.mark.asyncio
async def test_at_aggregate_cap_skips():
    # net_liq 100k * pct 1.0 = cap 100k; open already 100k -> skip
    guard = ExposureGuard(_policy(max_deployed_pct=1.0),
                          FakeGateway(net_liq=100_000),
                          trade_intent_store=object(),
                          exposure_fn=AsyncMock(return_value=100_000.0))
    result = await guard.run(_ctx())
    assert result.status == "skip"
    assert "exposure_cap_exceeded" in (result.reason or "")


@pytest.mark.asyncio
async def test_exposure_query_error_fails_safe():
    guard = ExposureGuard(_policy(), FakeGateway(),
                          trade_intent_store=object(),
                          exposure_fn=AsyncMock(side_effect=RuntimeError("db locked")))
    result = await guard.run(_ctx())
    assert result.status == "skip"
    assert "exposure_data_unavailable" in (result.reason or "")


@pytest.mark.asyncio
async def test_broker_down_fails_safe():
    guard = ExposureGuard(_policy(), FakeGateway(raise_=True),
                          trade_intent_store=object(),
                          exposure_fn=AsyncMock(return_value=0.0))
    result = await guard.run(_ctx())
    assert result.status == "skip"
    assert "exposure_data_unavailable" in (result.reason or "")


@pytest.mark.asyncio
async def test_within_limits_stashes_exposure_context():
    # cap 100k, open 30k -> headroom -> success + ctx hand-off to OrderSizer
    guard = ExposureGuard(_policy(max_deployed_pct=1.0),
                          FakeGateway(net_liq=100_000),
                          trade_intent_store=object(),
                          exposure_fn=AsyncMock(return_value=30_000.0))
    result = await guard.run(_ctx())
    assert result.status == "success"
    assert result.updates["open_exposure"] == pytest.approx(30_000.0)
    assert result.updates["aggregate_notional_cap"] == pytest.approx(100_000.0)
