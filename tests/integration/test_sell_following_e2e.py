"""End-to-end: a real classification of an explicit sell message routes through
SellClassifier -> SellFollower and closes the open shares position, while the
entry path is never taken."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from agent.traders.profile import TraderProfile
from agent.traders.registry import TraderRegistry
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.position_exit_store import PositionExitStore
from infra.ib.models import FillResult, FillStatus, BrokerContractRef
from skills.signal.trader_classifier import TraderClassifier
from skills.signal.sell_classifier import SellClassifier
from skills.execution.sell_follower import SellFollower


def _now():
    return datetime.now(timezone.utc).isoformat()


def _profile():
    return TraderProfile(
        handle="mystic", display_name="Mystic", discord_author_pattern="Mystic",
        alert_mention="@m", require_alert_mention=True, bot_authors_to_skip=(),
        auto_execute=True, size_in_message=False, prefer_message_size=True,
        classifier_model="claude-haiku-4-5", availability_phrases=(),
        conviction_examples=())


class FakeLLM:
    """Returns a combined response: the entry classifier reads is_entry/bucket
    (SKIP here), the sell classifier reads is_sell/scope (a real sell)."""
    def __init__(self, resp):
        self._resp = resp

    async def classify(self, *, system, model, messages):
        return self._resp


@pytest.mark.asyncio
async def test_explicit_sell_closes_position_and_skips_entry_path(db):
    intents = TradeIntentStore(db)
    exits = PositionExitStore(db)
    await intents.insert({
        "intent_id": "e0:AAPL:long", "event_id": "e0", "channel": "mystic",
        "ticker": "AAPL", "side": "long", "instrument_type": "equity",
        "conviction": "HIGH", "policy_state": "approved",
        "execution_state": "filled", "fill_qty": 100, "fill_price": 100.0,
        "filled_at": _now(), "signal_received_at": _now(),
        "intent_created_at": _now(), "created_at": _now(), "updated_at": _now()})

    registry = TraderRegistry([_profile()])
    llm = FakeLLM({
        # entry classifier view -> SKIP (not an entry)
        "is_entry": False, "bucket": "SKIP", "side": "none", "confidence": 0.95,
        "reason": "exit",
        # sell classifier view -> a full exit of AAPL
        "is_sell": True, "ticker": "AAPL", "scope": "full", "fraction": None})

    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD", qualified=True))
    gw.get_quote = AsyncMock(return_value=100.0)
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.cancel_order = AsyncMock(return_value=True)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=100, remaining_qty=0, avg_fill_price=99.5,
        last_status="Filled", status_timestamp=_now()))

    chain = [
        TraderClassifier(registry, llm),
        SellClassifier(registry, llm),
        SellFollower(gw, intents, exits, slippage_cap_pct=0.01,
                     fill_timeout=5.0, is_rth=lambda: True),
    ]
    ctx = Context(trace_id="t", event_id="evt-sell")
    ctx.update({"trader_handle": "mystic", "channel": "mystic",
                "full_message_text": "sold out of AAPL, done with it",
                "message_fingerprint": "fp-e2e"})

    terminal = None
    for skill in chain:
        result = await skill.run(ctx)
        if result.updates:
            ctx.update(result.updates)
        if result.status in ("skip", "fail"):
            terminal = (skill.name, result.status, result.reason)
            break

    # Entry classifier bucketed it SKIP; sell classifier flagged the sell.
    assert ctx.get("bucket") == "SKIP"
    assert ctx.get("action") == "sell"
    # SellFollower executed the sell and halted the entry path.
    assert terminal == ("SellFollower", "skip", "sell_followed")
    assert ctx.get("sell_total_sold_qty") == 100
    # Position fully closed, exit recorded.
    assert await exits.remaining_qty("e0:AAPL:long") == 0
    placed = gw.place_order.call_args[0][1]
    assert placed.action == "SELL" and placed.quantity == 100
