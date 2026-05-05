"""
End-to-end smoke test for the shares-plus-options execution chain.

Tests the new chain introduced in Task 19:
  ReferencePriceCapture → SizingResolver → EquityContractBuilder → OrderSizer
  → SharesMarketSubmitter → OptionsChaseGuard → ChainLookup → InstrumentMarketabilityGuard
  → ContractSelector → OrderSizer → OptionsMarketSubmitter

The test pre-creates an intent row (TradeIntentWriter role) and runs the
core execution sub-chain directly, bypassing the time-sensitive eligibility
guards (ExecutionEligibilityGuard, RthEntryGuard) so the test is not
coupled to wall-clock time.
"""
import pytest
import aiosqlite
from unittest.mock import AsyncMock, MagicMock
from datetime import date, timedelta

from agent.context import Context
from agent.policy import load_policy
from infra.storage.db import SCHEMA
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from infra.ib.models import (
    FillResult, FillStatus, BrokerContractRef, AccountSummary, OptionCandidate,
)
from skills.execution.reference_price_capture import ReferencePriceCapture
from skills.execution.sizing_resolver import SizingResolver
from skills.execution.equity_contract_builder import EquityContractBuilder
from skills.execution.order_sizer import OrderSizer
from skills.execution.shares_market_submitter import SharesMarketSubmitter
from skills.execution.options_chase_guard import OptionsChaseGuard
from skills.execution.chain_lookup import ChainLookup
from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
from skills.execution.contract_selector import ContractSelector
from skills.execution.options_market_submitter import OptionsMarketSubmitter


def _make_option_candidate(symbol: str, strike: float, expiry_days: int = 200) -> OptionCandidate:
    expiry_date = (date.today() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
    expiry_compact = expiry_date.replace("-", "")
    ref = BrokerContractRef(
        symbol=symbol, sec_type="OPT", exchange="SMART", currency="USD",
        strike=strike, expiry=expiry_compact, right="C", qualified=True,
    )
    return OptionCandidate(
        symbol=symbol, expiry=expiry_date, strike=strike, right="C",
        bid=4.95, ask=5.0, mid=4.975, spread_pct=0.01,
        open_interest=1000, volume=500, multiplier=100,
        contract_ref=ref,
    )


@pytest.mark.asyncio
async def test_high_signal_fires_shares_then_options():
    """
    Happy-path: HIGH-bucket signal triggers shares fill, trim ladder is armed,
    and the options sub-chain proceeds to fill an option contract.
    """
    # --- In-memory DB with full schema ---
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    intents = TradeIntentStore(db)
    trims = TrimLadderStore(db)

    # --- Load real policy (gives us trim_ladder rungs, sizing, etc.) ---
    policy = load_policy("config/policy.yaml")

    # --- Pre-create the intent row (normally written by TradeIntentWriter) ---
    intent_id = "intent-e2e-test"
    now_iso = "2026-05-05T14:00:00+00:00"
    await intents.insert({
        "intent_id": intent_id,
        "event_id": "e-test",
        "channel": "stock-talk-portfolio",
        "ticker": "AAPL",
        "side": "long",
        "instrument_type": "equity",
        "parent_intent_id": None,
        "expiry": None,
        "strike": None,
        "right": None,
        "conviction": "HIGH",
        "analysis_confidence": 0.9,
        "ambiguity_flags": None,
        "rationale": "deep thesis",
        "ticker_raw": "AAPL",
        "side_raw": "long",
        "conviction_raw": "HIGH",
        "reference_spot_price": None,
        "reference_spot_timestamp": None,
        "policy_state": "approved",
        "execution_mode": "execute_now",
        "execution_state": None,
        "outbox_status": None,
        "signal_received_at": now_iso,
        "intent_created_at": now_iso,
        "created_at": now_iso,
        "updated_at": now_iso,
    })

    # --- Stub gateway ---
    # get_quote call sequence:
    #   1. ReferencePriceCapture → 100.0
    #   2. OrderSizer (equity) → 100.0
    #   3. OptionsChaseGuard → 101.0 (within 10% threshold → options proceed)
    equity_ref = BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        qualified=True,
    )
    candidate = _make_option_candidate("AAPL", strike=95.0)

    gw = MagicMock()
    gw.get_quote = AsyncMock(side_effect=[100.0, 100.0, 101.0])
    gw.qualify_equity = AsyncMock(return_value=equity_ref)
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=200_000.0, net_liquidation=100_000.0, currency="USD",
    ))
    gw.get_chain = AsyncMock(return_value=[candidate])
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(side_effect=[
        FillResult(
            status=FillStatus.FILLED, broker_order_id="shares-ord-1",
            perm_id=1, submitted_qty=200, filled_qty=200, remaining_qty=0,
            avg_fill_price=100.0, last_status="Filled",
            status_timestamp="2026-05-05T14:00:01+00:00",
        ),
        FillResult(
            status=FillStatus.FILLED, broker_order_id="options-ord-1",
            perm_id=2, submitted_qty=50, filled_qty=50, remaining_qty=0,
            avg_fill_price=5.0, last_status="Filled",
            status_timestamp="2026-05-05T14:00:02+00:00",
        ),
    ])

    # execution_store stub — only _conn is used by ChainLookup
    execution_store = MagicMock()
    execution_store._conn = db

    # Trim rungs from policy
    rungs = [
        (i + 1, r.threshold_pct, r.trim_pct)
        for i, r in enumerate(policy.execution.trim_ladder.rungs)
    ]

    # --- Build the core execution sub-chain (post eligibility-guards) ---
    chain = [
        ReferencePriceCapture(gw),
        SizingResolver(policy.execution),
        EquityContractBuilder(gw),
        OrderSizer(gw, margin_multiplier=policy.execution.margin_multiplier),
        SharesMarketSubmitter(
            gw, intents, trims,
            fill_timeout=policy.execution.fill_wait_timeout_seconds,
            trim_rungs=rungs,
        ),
        OptionsChaseGuard(
            gw, threshold_pct=policy.execution.options_chase_threshold_pct,
        ),
        ChainLookup(gw, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(gw, margin_multiplier=policy.execution.margin_multiplier),
        OptionsMarketSubmitter(
            gw, intents,
            fill_timeout=policy.execution.fill_wait_timeout_seconds,
        ),
    ]

    # --- Context ---
    ctx = Context(trace_id="t-e2e", event_id="e-test")
    ctx.update({
        "intent_id": intent_id,
        "channel": "stock-talk-portfolio",
        "ticker": "AAPL",
        "side": "long",
        "bucket": "HIGH",
        "execution_session": "rth",
        "spot_price": 100.0,
    })

    # --- Run the chain ---
    for skill in chain:
        result = await skill.run(ctx)
        if result.status == "fail":
            pytest.fail(f"Chain failed at {skill.name}: {result.reason}")
        if result.updates:
            ctx.update(result.updates)
        if result.status == "skip":
            # OptionsChaseGuard or later may skip — that's acceptable in theory
            # but in this happy-path test we expect the full chain to succeed.
            pytest.fail(
                f"Chain unexpectedly skipped at {skill.name}: {result.reason}"
            )

    # --- Assertions ---
    # Shares fill captured
    assert ctx.get("shares_fill_qty") == 200
    assert ctx.get("shares_fill_price") == 100.0

    # Trim ladder armed for the shares intent
    rows = await trims.unfired_for_intent(intent_id)
    assert len(rows) == 2, f"Expected 2 trim rungs, got {len(rows)}"
    assert rows[0]["rung"] == 1
    assert rows[1]["rung"] == 2

    # Options fill captured
    assert ctx.get("options_fill_qty") == 50

    await db.close()


@pytest.mark.asyncio
async def test_options_chase_guard_skips_when_price_chased():
    """
    When the spot price has moved more than the chase threshold since the
    reference quote, the options sub-chain should be skipped gracefully.
    Shares fill still succeeds and trim ladder is armed.
    """
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    intents = TradeIntentStore(db)
    trims = TrimLadderStore(db)

    policy = load_policy("config/policy.yaml")

    intent_id = "intent-chase-test"
    now_iso = "2026-05-05T14:00:00+00:00"
    await intents.insert({
        "intent_id": intent_id,
        "event_id": "e-chase",
        "channel": "stock-talk-portfolio",
        "ticker": "AAPL",
        "side": "long",
        "instrument_type": "equity",
        "parent_intent_id": None,
        "expiry": None, "strike": None, "right": None,
        "conviction": "HIGH",
        "analysis_confidence": 0.9,
        "ambiguity_flags": None,
        "rationale": "deep thesis",
        "ticker_raw": "AAPL",
        "side_raw": "long",
        "conviction_raw": "HIGH",
        "reference_spot_price": None,
        "reference_spot_timestamp": None,
        "policy_state": "approved",
        "execution_mode": "execute_now",
        "execution_state": None,
        "outbox_status": None,
        "signal_received_at": now_iso,
        "intent_created_at": now_iso,
        "created_at": now_iso,
        "updated_at": now_iso,
    })

    equity_ref = BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        qualified=True,
    )

    gw = MagicMock()
    # reference = 100, after-shares quote = 115 (15% > 10% threshold → skip options)
    gw.get_quote = AsyncMock(side_effect=[100.0, 100.0, 115.0])
    gw.qualify_equity = AsyncMock(return_value=equity_ref)
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=200_000.0, net_liquidation=100_000.0, currency="USD",
    ))
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="shares-ord-chase",
        perm_id=10, submitted_qty=200, filled_qty=200, remaining_qty=0,
        avg_fill_price=100.0, last_status="Filled",
        status_timestamp="2026-05-05T14:00:01+00:00",
    ))

    execution_store = MagicMock()
    execution_store._conn = db

    rungs = [
        (i + 1, r.threshold_pct, r.trim_pct)
        for i, r in enumerate(policy.execution.trim_ladder.rungs)
    ]

    chain = [
        ReferencePriceCapture(gw),
        SizingResolver(policy.execution),
        EquityContractBuilder(gw),
        OrderSizer(gw, margin_multiplier=policy.execution.margin_multiplier),
        SharesMarketSubmitter(
            gw, intents, trims,
            fill_timeout=policy.execution.fill_wait_timeout_seconds,
            trim_rungs=rungs,
        ),
        OptionsChaseGuard(
            gw, threshold_pct=policy.execution.options_chase_threshold_pct,
        ),
    ]

    ctx = Context(trace_id="t-chase", event_id="e-chase")
    ctx.update({
        "intent_id": intent_id,
        "channel": "stock-talk-portfolio",
        "ticker": "AAPL",
        "side": "long",
        "bucket": "HIGH",
        "execution_session": "rth",
        "spot_price": 100.0,
    })

    partial_set_by = None
    for skill in chain:
        result = await skill.run(ctx)
        if result.status == "fail":
            pytest.fail(f"Chain failed at {skill.name}: {result.reason}")
        if result.updates:
            ctx.update(result.updates)
            if "partial_execution_reason" in result.updates and partial_set_by is None:
                partial_set_by = skill.name
        if result.status == "skip":
            break

    # Shares filled
    assert ctx.get("shares_fill_qty") == 200

    # Trim ladder armed
    rows = await trims.unfired_for_intent(intent_id)
    assert len(rows) == 2

    # Options leg recorded a chase-guard partial reason rather than failing
    # the trace — shares-filled trades must not be marked failed/skipped.
    assert partial_set_by == "OptionsChaseGuard"
    assert "options_chase_skip" in ctx.get("partial_execution_reason")

    await db.close()
