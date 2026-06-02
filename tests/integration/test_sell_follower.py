import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.position_exit_store import PositionExitStore
from infra.ib.models import FillResult, FillStatus, BrokerContractRef
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution.sell_follower import SellFollower


def _now():
    return datetime.now(timezone.utc).isoformat()


def _filled_intent(intent_id, *, channel="mystic", ticker="AAPL", fill_qty=100):
    now = _now()
    return {
        "intent_id": intent_id, "event_id": intent_id.split(":")[0],
        "channel": channel, "ticker": ticker, "side": "long",
        "instrument_type": "equity", "conviction": "HIGH", "policy_state": "approved",
        "execution_state": "filled", "fill_qty": fill_qty, "fill_price": 100.0,
        "filled_at": now, "signal_received_at": now, "intent_created_at": now,
        "created_at": now, "updated_at": now,
    }


def _gw(fill_qty=None, status=FillStatus.FILLED):
    gw = MagicMock()
    gw.qualify_equity = AsyncMock(return_value=BrokerContractRef(
        symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD", qualified=True))
    gw.get_quote = AsyncMock(return_value=100.0)
    gw.place_order = AsyncMock(return_value=MagicMock())
    gw.cancel_order = AsyncMock(return_value=True)
    return gw


def _ctx(*, scope="full", fraction=1.0, ticker="AAPL", fp="fp-1"):
    c = Context(trace_id="t", event_id="evt-sell")
    c.update({"action": "sell", "sell_ticker": ticker, "sell_scope": scope,
              "sell_fraction": fraction, "channel": "mystic",
              "message_fingerprint": fp})
    return c


def _follower(gw, db, *, is_rth=True):
    return SellFollower(
        gw, TradeIntentStore(db), PositionExitStore(db),
        slippage_cap_pct=0.01, fill_timeout=5.0, is_rth=lambda: is_rth)


@pytest.mark.asyncio
async def test_passthrough_for_non_sell(db):
    gw = _gw()
    ctx = Context(trace_id="t", event_id="e")
    ctx.update({"bucket": "HIGH"})  # an entry
    result = await _follower(gw, db).run(ctx)
    assert result.status == "success"
    gw.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_exit_sells_all_remaining(db):
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=100))
    gw = _gw()
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=100, remaining_qty=0, avg_fill_price=99.5,
        last_status="Filled", status_timestamp=_now()))
    result = await _follower(gw, db).run(_ctx(scope="full"))
    assert result.status == "skip"
    assert result.reason == "sell_followed"
    placed = gw.place_order.call_args[0][1]
    assert placed.action == "SELL"
    assert placed.order_type == "LMT"
    assert placed.quantity == 100
    assert placed.limit_price == 99.0  # floor(100 * 0.99)
    exits = PositionExitStore(db)
    assert await exits.remaining_qty("e1:AAPL:long") == 0


@pytest.mark.asyncio
async def test_partial_exit_uses_aggregate_fraction_oldest_first(db):
    intents = TradeIntentStore(db)
    # Two open lots: 100 (older) + 60 (newer). "sold half" -> 80 across both.
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=100))
    await intents.insert(_filled_intent("e2:AAPL:long", fill_qty=60))
    gw = _gw()
    # Each place_order fully fills its requested qty.
    fills = []

    async def fill_for(trade, timeout):
        q = gw.place_order.call_args[0][1].quantity
        fills.append(q)
        return FillResult(status=FillStatus.FILLED, broker_order_id="o", perm_id=1,
                          submitted_qty=q, filled_qty=q, remaining_qty=0,
                          avg_fill_price=99.0, last_status="Filled",
                          status_timestamp=_now())
    gw.wait_fill = AsyncMock(side_effect=fill_for)
    result = await _follower(gw, db).run(_ctx(scope="partial", fraction=0.5))
    assert result.status == "skip"
    # agg_remaining=160, target=floor(160*0.5)=80; oldest-first: 80 from lot e1.
    assert fills == [80]
    exits = PositionExitStore(db)
    assert await exits.remaining_qty("e1:AAPL:long") == 20
    assert await exits.remaining_qty("e2:AAPL:long") == 60


@pytest.mark.asyncio
async def test_partial_spills_to_second_lot(db):
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=50))
    await intents.insert(_filled_intent("e2:AAPL:long", fill_qty=50))
    gw = _gw()

    async def fill_for(trade, timeout):
        q = gw.place_order.call_args[0][1].quantity
        return FillResult(status=FillStatus.FILLED, broker_order_id="o", perm_id=1,
                          submitted_qty=q, filled_qty=q, remaining_qty=0,
                          avg_fill_price=99.0, last_status="Filled",
                          status_timestamp=_now())
    gw.wait_fill = AsyncMock(side_effect=fill_for)
    # full exit of 100 across two 50-lots -> two orders of 50.
    await _follower(gw, db).run(_ctx(scope="full"))
    assert gw.place_order.await_count == 2
    exits = PositionExitStore(db)
    assert await exits.remaining_qty("e1:AAPL:long") == 0
    assert await exits.remaining_qty("e2:AAPL:long") == 0


@pytest.mark.asyncio
async def test_no_open_position_skips_without_claiming(db):
    gw = _gw()
    result = await _follower(gw, db).run(_ctx())
    assert result.status == "skip"
    assert result.reason == "no_open_position"
    gw.place_order.assert_not_awaited()
    # Not claimed -> a later legit sell with the same fingerprint can still act.
    assert await PositionExitStore(db).claim_sell_event("fp-1", "x") is True


@pytest.mark.asyncio
async def test_reposted_partial_sell_is_deduped_by_claim(db):
    # A PARTIAL exit leaves remaining>0, so the fingerprint CLAIM (not the
    # remaining-qty guard) is what blocks a reworded/redelivered repost.
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=100))
    gw = _gw()

    async def fill_for(trade, timeout):
        q = gw.place_order.call_args[0][1].quantity
        return FillResult(status=FillStatus.FILLED, broker_order_id="o", perm_id=1,
                          submitted_qty=q, filled_qty=q, remaining_qty=0,
                          avg_fill_price=99.0, last_status="Filled",
                          status_timestamp=_now())
    gw.wait_fill = AsyncMock(side_effect=fill_for)
    follower = _follower(gw, db)
    r1 = await follower.run(_ctx(scope="partial", fraction=0.5, fp="same-fp"))
    assert r1.reason == "sell_followed"
    # Re-deliver the same content (new event_id, same fingerprint).
    ctx2 = _ctx(scope="partial", fraction=0.5, fp="same-fp")
    ctx2.update({"event_id": "evt-sell-2"})
    r2 = await follower.run(ctx2)
    assert r2.reason == "sell_already_followed"
    assert gw.place_order.await_count == 1  # only sold once


@pytest.mark.asyncio
async def test_full_exit_repost_finds_no_position(db):
    # After a full exit, remaining is 0, so a repost is a no-op via the
    # remaining-qty guard (the other idempotency mechanism).
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=100))
    gw = _gw()
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=100, remaining_qty=0, avg_fill_price=99.5,
        last_status="Filled", status_timestamp=_now()))
    follower = _follower(gw, db)
    assert (await follower.run(_ctx(fp="fp-a"))).reason == "sell_followed"
    ctx2 = _ctx(fp="fp-b")  # even a different fingerprint -> nothing left to sell
    assert (await follower.run(ctx2)).reason == "no_open_position"
    assert gw.place_order.await_count == 1


@pytest.mark.asyncio
async def test_outside_rth_skips_without_claim(db):
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long"))
    gw = _gw()
    result = await _follower(gw, db, is_rth=False).run(_ctx())
    assert result.status == "skip"
    assert result.reason == "sell_outside_rth"
    gw.place_order.assert_not_awaited()
    assert await PositionExitStore(db).claim_sell_event("fp-1", "x") is True  # unclaimed


@pytest.mark.asyncio
async def test_zero_fill_cancels_residual_and_records_zero(db):
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=100))
    gw = _gw()
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.TIMED_OUT_PENDING, broker_order_id="o1", perm_id=1,
        submitted_qty=100, filled_qty=0, remaining_qty=100, avg_fill_price=None,
        last_status="Submitted", status_timestamp=_now()))
    result = await _follower(gw, db).run(_ctx())
    assert result.status == "skip"
    # A zero-fill must NOT report success ('sell_followed') — it's a missed sell
    # that needs an operator alert, not a green digest.
    assert result.reason == "sell_zero_fill"
    gw.cancel_order.assert_awaited_once()
    # Nothing sold -> still fully held.
    assert await PositionExitStore(db).remaining_qty("e1:AAPL:long") == 100


@pytest.mark.asyncio
async def test_partial_then_broker_down_is_executed_not_skipped(db):
    # First lot sells; the second lot's order hits a broker outage. Shares
    # really sold -> reported as an executed (partial) sell, claim kept.
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=50))
    await intents.insert(_filled_intent("e2:AAPL:long", fill_qty=50))
    gw = _gw()
    calls = {"n": 0}

    async def place(contract, order, coid):
        calls["n"] += 1
        if calls["n"] == 2:
            raise IBGatewayUnavailable("down")
        return MagicMock()
    gw.place_order = AsyncMock(side_effect=place)
    gw.wait_fill = AsyncMock(return_value=FillResult(
        status=FillStatus.FILLED, broker_order_id="o", perm_id=1,
        submitted_qty=50, filled_qty=50, remaining_qty=0, avg_fill_price=99.0,
        last_status="Filled", status_timestamp=_now()))
    result = await _follower(gw, db).run(_ctx(scope="full", fp="fp-pb"))
    assert result.status == "skip"
    assert result.reason.startswith("sell_partial_broker_unavailable")
    assert result.updates["sell_total_sold_qty"] == 50  # first lot really sold
    # Claim is NOT released (the sold 50 must never be re-sold).
    assert await PositionExitStore(db).claim_sell_event("fp-pb", "x") is False


@pytest.mark.asyncio
async def test_broker_unavailable_with_no_sale_releases_claim(db):
    intents = TradeIntentStore(db)
    await intents.insert(_filled_intent("e1:AAPL:long", fill_qty=100))
    gw = _gw()
    gw.place_order = AsyncMock(side_effect=IBGatewayUnavailable("down"))
    result = await _follower(gw, db).run(_ctx(fp="fp-retry"))
    assert result.status == "skip"
    assert "broker_unavailable" in result.reason
    # Released so a repost can retry (nothing was sold).
    assert await PositionExitStore(db).claim_sell_event("fp-retry", "x") is True
