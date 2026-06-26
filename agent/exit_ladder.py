from __future__ import annotations
import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution._pricing import marketable_sell_limit

EASTERN = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


def _round_half_up_min1(n: float) -> int:
    rounded = int(math.floor(n + 0.5))
    return max(1, rounded)


async def fire_rung_if_crossed(
    *, gw, trim_store, exits_store, intent_id: str, ticker: str,
    avg_fill_price: float, original_qty: int,
    rung: int, threshold_pct: float, trim_pct: float,
    current_price: float, slippage_cap_pct: float = 0.01,
) -> bool:
    threshold_price = avg_fill_price * (1.0 + threshold_pct)
    if current_price < threshold_price:
        return False

    # Never trim more than the shares actually still held. remaining_qty nets
    # prior trim fires AND trader-followed exits against the original fill, so a
    # position we've already (fully or partly) exited cannot be re-sold into a
    # short. Checked BEFORE the claim so an un-sellable rung is left for a later
    # tick rather than silently consumed.
    remaining_held = await exits_store.remaining_qty(intent_id)
    if remaining_held <= 0:
        return False

    # Claim the rung before placing the order so an overlapping tick cannot
    # double-fire while wait_fill is in-flight.
    started_at = datetime.now(timezone.utc).isoformat()
    if not await trim_store.claim_for_fire(intent_id, rung, started_at):
        return False  # another tick already owns this rung

    trim_qty = min(_round_half_up_min1(original_qty * trim_pct), remaining_held)
    contract = await gw.qualify_equity(ticker)
    # Marketable SELL limit: floor slippage at current_price * (1 - cap) so a
    # thin book can't dump the trim well below market (was a naked MKT sell).
    limit = marketable_sell_limit(current_price, slippage_cap_pct)
    order = PreparedOrder(action="SELL", quantity=trim_qty, order_type="LMT",
                          limit_price=limit, tif="DAY")
    client_order_id = f"{intent_id}:trim:R{rung}"
    try:
        trade = await gw.place_order(contract, order, client_order_id)
        fill = await gw.wait_fill(trade, timeout=30.0)
    except IBGatewayUnavailable as exc:
        logger.error("trim sell broker unavailable: %s", exc)
        await trim_store.release_claim(intent_id, rung)
        return False

    if fill.filled_qty <= 0:
        # Nothing sold (limit didn't fill / rejected): cancel any residual and
        # release the rung so a later tick can retry — do NOT consume it.
        await _cancel_trim_residual(gw, trade, fill)
        await trim_store.release_claim(intent_id, rung)
        return False

    if fill.status != FillStatus.FILLED:
        # Partial: cancel the residual working sell order, record what sold.
        await _cancel_trim_residual(gw, trade, fill)
        logger.warning("trim partial fill %s: %d/%d sold, residual cancelled",
                       client_order_id, fill.filled_qty, trim_qty)

    await trim_store.record_fire(
        intent_id=intent_id, rung=rung,
        fired_at=datetime.now(timezone.utc).isoformat(),
        fire_price=current_price,
        sold_qty=fill.filled_qty,
        sold_avg_price=fill.avg_fill_price,
        broker_order_ref=fill.broker_order_id,
    )
    return True


async def _cancel_trim_residual(gw, trade, fill) -> None:
    """Best-effort cancel of a trim sell's remainder. Never raise — a failed
    cancel must not mask a recorded fill."""
    if fill.status == FillStatus.FILLED:
        return
    try:
        await gw.cancel_order(trade)
    except Exception:
        logger.exception("trim residual cancel failed (order may rest at IB)")


def _in_rth(now_eastern: datetime) -> bool:
    """Return True if now_eastern falls within Regular Trading Hours (9:30–16:00 ET)."""
    h, m = now_eastern.hour, now_eastern.minute
    return (h == 9 and m >= 30) or (10 <= h < 16)


class ExitLadder:
    def __init__(self, gateway, intent_store, trim_store, exits_store, *,
                 poll_interval_seconds: int, slippage_cap_pct: float = 0.01):
        self._gw = gateway
        self._intents = intent_store
        self._trims = trim_store
        self._exits = exits_store
        self._interval = poll_interval_seconds
        self._cap = slippage_cap_pct
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception:
                logger.exception("exit ladder tick failed")
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        # Only fire during RTH
        if not _in_rth(datetime.now(EASTERN)):
            return

        unfired = await self._trims.all_unfired()
        by_intent = defaultdict(list)
        for row in unfired:
            by_intent[row["intent_id"]].append(row)

        for intent_id, rungs in by_intent.items():
            intent = await self._intents.get(intent_id)
            if not intent or intent["execution_state"] != "filled":
                continue
            # Skip positions already fully exited (trader-followed sell or prior
            # trims): nothing left to trim, and firing would short the position.
            if await self._exits.remaining_qty(intent_id) <= 0:
                continue
            try:
                current_price = await self._gw.get_quote(intent["ticker"])
            except IBGatewayUnavailable:
                continue
            for r in sorted(rungs, key=lambda x: x["rung"]):
                fired = await fire_rung_if_crossed(
                    gw=self._gw, trim_store=self._trims, exits_store=self._exits,
                    intent_id=intent_id, ticker=intent["ticker"],
                    avg_fill_price=intent["fill_price"],
                    original_qty=intent["fill_qty"],
                    rung=r["rung"], threshold_pct=r["threshold_pct"],
                    trim_pct=r["trim_pct"],
                    current_price=current_price,
                    slippage_cap_pct=self._cap,
                )
                if not fired:
                    break  # rungs ordered; if R1 didn't fire, R2 won't either
