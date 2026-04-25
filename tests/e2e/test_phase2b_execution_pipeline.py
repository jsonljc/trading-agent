import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock
from datetime import date, timedelta
from agent.context import Context
from agent.orchestrator import Orchestrator
from infra.storage.trace_store import TraceStore
from infra.storage.execution_store import ExecutionStore
from infra.storage.trade_intent_store import TradeIntentStore
from skills.execution.trade_intent_writer import TradeIntentWriter
from skills.execution.channel_policy_guard import ChannelPolicyGuard
from skills.execution.cooldown_guard import CooldownGuard
from infra.ib.models import (
    OptionCandidate, BrokerContractRef, AccountSummary,
    FillResult, FillStatus, ExecutionMode,
)
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
from skills.execution.chain_lookup import ChainLookup
from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
from skills.execution.contract_selector import ContractSelector
from skills.execution.order_sizer import OrderSizer
from skills.execution.order_pricer import OrderPricer
from skills.execution.order_submitter import OrderSubmitter
from skills.execution.fill_waiter import FillWaiter
from datetime import datetime
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def _policy():
    p = MagicMock()
    p.market_hours.rth_start = "09:30"
    p.market_hours.rth_end = "16:00"
    p.market_hours.stock_premarket_allowed = True
    p.market_hours.stock_premarket_start = "04:00"
    p.market_hours.stock_afterhours_queue = True
    p.instrument_policy.min_expiry_days = 30
    p.instrument_policy.strike_policy = "closest_itm_call"
    p.pricing_policy_guards.min_bid = 0.01
    p.pricing_policy_guards.max_spread_pct = 0.40
    p.pricing_policy.option_spread_fraction = 0.25
    p.pricing_policy.stock_buffer_pct = 0.001
    p.sizing_policy.low_conviction_pct = 0.05
    p.sizing_policy.high_conviction_pct = 0.10
    p.execution.fill_wait_timeout_seconds = 1.0
    p.execution.max_equity_price = 500.0
    return p


def _candidate(strike=150.0, expiry_days=200):
    expiry = (date.today() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
    ref = BrokerContractRef(symbol="AAPL", sec_type="OPT", exchange="SMART",
                             currency="USD", expiry=expiry.replace("-",""),
                             strike=strike, right="C", qualified=True)
    return OptionCandidate(symbol="AAPL", expiry=expiry, strike=strike, right="C",
                            bid=5.0, ask=5.5, mid=5.25, spread_pct=0.09,
                            open_interest=100, volume=50, multiplier=100,
                            contract_ref=ref)


def _gateway():
    fake_trade = MagicMock()
    fake_trade.order.orderId = "IB-TEST-1"
    fake_trade.order.permId = 42
    gw = MagicMock()
    gw.get_chain = AsyncMock(return_value=[_candidate(strike=150.0)])
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=50_000.0, net_liquidation=50_000.0, currency="USD"
    ))
    gw.get_quote = AsyncMock(return_value=155.0)
    gw.qualify = AsyncMock(side_effect=lambda ref: setattr(ref, 'qualified', True) or ref)
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="IB-TEST-1", perm_id=42,
        submitted_qty=9, filled_qty=9, remaining_qty=0,
        avg_fill_price=5.26, last_status="Filled",
        status_timestamp="2026-04-22T10:00:00+00:00",
    ))
    return gw


def _rth_time():
    return datetime(2026, 4, 22, 10, 0, tzinfo=ET)


def _policy_with_channels(auto_execute: bool = True):
    p = _policy()
    ch_cfg = MagicMock()
    ch_cfg.auto_execute = auto_execute
    p.watched_channels = {"mystic": ch_cfg}
    p.cooldown_policy.enabled = False  # disable cooldown so it doesn't interfere
    return p


@pytest.mark.asyncio
async def test_full_execution_pipeline_happy_path(db):
    policy = _policy()
    gateway = _gateway()
    execution_store = ExecutionStore(db)
    trace_store = TraceStore(db)

    chain = [
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id=str(uuid.uuid4())[:12], event_id="evt-1")
    ctx.update({
        "signal_id": "sig-1",
        "ticker": "AAPL",
        "conviction_bucket": "high",
        "spot_price": 152.0,  # 150 is ITM
    })

    result_ctx = await orch.run(ctx)

    assert result_ctx.get("fill_status") == FillStatus.FILLED.value
    assert result_ctx.get("filled_qty") == 9
    assert result_ctx.get("execution_mode") == ExecutionMode.EXECUTE_NOW.value

    # Verify execution row persisted
    async with db.execute("SELECT status, filled_qty FROM executions") as cur:
        row = await cur.fetchone()
    assert row["status"] == "filled"
    assert row["filled_qty"] == 9


@pytest.mark.asyncio
async def test_broker_unavailable_fails_pipeline(db):
    policy = _policy()
    gateway = _gateway()
    gateway.get_chain = AsyncMock(side_effect=IBGatewayUnavailable("circuit open"))
    execution_store = ExecutionStore(db)
    trace_store = TraceStore(db)

    chain = [
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id="t2", event_id="e2")
    ctx.update({"signal_id": "sig-2", "ticker": "AAPL", "conviction_bucket": "high", "spot_price": 152.0})

    await orch.run(ctx)

    async with db.execute("SELECT status FROM work_traces WHERE trace_id='t2'") as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_intent_row_created_on_happy_path(db):
    policy = _policy_with_channels(auto_execute=True)
    gateway = _gateway()
    execution_store = ExecutionStore(db)
    intent_store = TradeIntentStore(db)
    trace_store = TraceStore(db)

    chain = [
        TradeIntentWriter(intent_store),
        ChannelPolicyGuard(policy, intent_store),
        CooldownGuard(policy, intent_store),
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id=str(uuid.uuid4())[:12], event_id="evt-intent-1")
    ctx.update({
        "signal_id": "sig-intent-1",
        "ticker": "AAPL",
        "conviction_bucket": "high",
        "spot_price": 152.0,
        "channel": "mystic",
        "intent": "LONG_SIGNAL",
        "received_at": "2026-04-24T14:00:00+00:00",
    })

    await orch.run(ctx)

    intent_id = "evt-intent-1:AAPL:long"
    row = await intent_store.get(intent_id)
    assert row is not None
    assert row["ticker"] == "AAPL"
    assert row["policy_state"] == "approved"


@pytest.mark.asyncio
async def test_channel_blocked_skips_execution(db):
    policy = _policy_with_channels(auto_execute=False)
    gateway = _gateway()
    execution_store = ExecutionStore(db)
    intent_store = TradeIntentStore(db)
    trace_store = TraceStore(db)

    chain = [
        TradeIntentWriter(intent_store),
        ChannelPolicyGuard(policy, intent_store),
        CooldownGuard(policy, intent_store),
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store, timeout=1.0),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id=str(uuid.uuid4())[:12], event_id="evt-blocked-1")
    ctx.update({
        "signal_id": "sig-blocked-1",
        "ticker": "AAPL",
        "conviction_bucket": "high",
        "spot_price": 152.0,
        "channel": "mystic",
        "intent": "LONG_SIGNAL",
        "received_at": "2026-04-24T14:00:00+00:00",
    })

    await orch.run(ctx)

    intent_id = "evt-blocked-1:AAPL:long"
    row = await intent_store.get(intent_id)
    assert row is not None
    assert row["policy_state"] == "channel_blocked"

    # No execution row should exist
    async with db.execute("SELECT count(*) as n FROM executions") as cur:
        count_row = await cur.fetchone()
    assert count_row["n"] == 0
