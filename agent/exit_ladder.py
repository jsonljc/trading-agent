from __future__ import annotations
import asyncio
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable

EASTERN = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


def _round_half_up_min1(n: float) -> int:
    rounded = int(math.floor(n + 0.5))
    return max(1, rounded)


async def fire_rung_if_crossed(
    *, gw, trim_store, intent_id: str, ticker: str,
    avg_fill_price: float, original_qty: int,
    rung: int, threshold_pct: float, trim_pct: float,
    current_price: float,
) -> bool:
    threshold_price = avg_fill_price * (1.0 + threshold_pct)
    if current_price < threshold_price:
        return False

    # Claim the rung before placing the order so an overlapping tick cannot
    # double-fire while wait_fill is in-flight.
    started_at = datetime.now(timezone.utc).isoformat()
    if not await trim_store.claim_for_fire(intent_id, rung, started_at):
        return False  # another tick already owns this rung

    trim_qty = _round_half_up_min1(original_qty * trim_pct)
    contract = await gw.qualify_equity(ticker)
    order = PreparedOrder(action="SELL", quantity=trim_qty, order_type="MKT",
                          limit_price=None, tif="DAY")
    client_order_id = f"{intent_id}:trim:R{rung}"
    try:
        trade = await gw.place_order(contract, order, client_order_id)
        fill = await gw.wait_fill(trade, timeout=30.0)
    except IBGatewayUnavailable as exc:
        logger.error("trim sell broker unavailable: %s", exc)
        await trim_store.release_claim(intent_id, rung)
        return False

    await trim_store.record_fire(
        intent_id=intent_id, rung=rung,
        fired_at=datetime.now(timezone.utc).isoformat(),
        fire_price=current_price,
        sold_qty=fill.filled_qty if fill.status == FillStatus.FILLED else 0,
        sold_avg_price=fill.avg_fill_price,
        broker_order_ref=fill.broker_order_id,
    )
    return True


def _in_rth(now_eastern: datetime) -> bool:
    """Return True if now_eastern falls within Regular Trading Hours (9:30–16:00 ET)."""
    h, m = now_eastern.hour, now_eastern.minute
    return (h == 9 and m >= 30) or (10 <= h < 16)


class ExitLadder:
    def __init__(self, gateway, intent_store, trim_store, *,
                 poll_interval_seconds: int):
        self._gw = gateway
        self._intents = intent_store
        self._trims = trim_store
        self._interval = poll_interval_seconds
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
            try:
                current_price = await self._gw.get_quote(intent["ticker"])
            except IBGatewayUnavailable:
                continue
            for r in sorted(rungs, key=lambda x: x["rung"]):
                fired = await fire_rung_if_crossed(
                    gw=self._gw, trim_store=self._trims,
                    intent_id=intent_id, ticker=intent["ticker"],
                    avg_fill_price=intent["fill_price"],
                    original_qty=intent["fill_qty"],
                    rung=r["rung"], threshold_pct=r["threshold_pct"],
                    trim_pct=r["trim_pct"],
                    current_price=current_price,
                )
                if not fired:
                    break  # rungs ordered; if R1 didn't fire, R2 won't either
