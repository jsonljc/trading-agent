from __future__ import annotations
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution._pricing import marketable_sell_limit

logger = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")


def _in_rth(now_eastern: datetime) -> bool:
    h, m = now_eastern.hour, now_eastern.minute
    return (h == 9 and m >= 30) or (10 <= h < 16)


async def follow_sell_position(
    *, gw, exits_store, fingerprint: str, event_id: str | None, intent_id: str,
    channel: str | None, ticker: str, qty: int, scope: str,
    slippage_cap_pct: float, fill_timeout: float,
) -> int:
    """Submit a marketable-limit SELL for `qty` shares of one position and record
    the exit. Returns the actual quantity sold (0 on a non-fill). Mirrors the
    trim ladder's partial-fill discipline: cancel any residual, record the real
    fill. Raises IBGatewayUnavailable to the caller (which owns the claim)."""
    contract = await gw.qualify_equity(ticker)
    price = await gw.get_quote(ticker)
    limit = marketable_sell_limit(price, slippage_cap_pct)
    order = PreparedOrder(action="SELL", quantity=qty, order_type="LMT",
                          limit_price=limit, tif="DAY")
    client_order_id = f"{intent_id}:exit:{fingerprint[:16]}"
    trade = await gw.place_order(contract, order, client_order_id)
    fill = await gw.wait_fill(trade, timeout=fill_timeout)

    sold = int(fill.filled_qty) if fill.filled_qty and fill.filled_qty > 0 else 0
    if fill.status != FillStatus.FILLED:
        # Cancel any residual working order (zero-fill or partial).
        try:
            await gw.cancel_order(trade)
        except Exception:
            logger.exception("sell residual cancel failed (order may rest at IB)")

    await exits_store.record_exit(
        fingerprint=fingerprint, event_id=event_id, intent_id=intent_id,
        channel=channel, ticker=ticker, scope=scope, requested_qty=qty,
        sold_qty=sold, sold_avg_price=fill.avg_fill_price,
        broker_order_ref=fill.broker_order_id, reason="follow_sell")
    if sold < qty:
        logger.warning("follow_sell %s: sold %d/%d (%s)", intent_id, sold, qty,
                       fill.last_status)
    return sold


class SellFollower(Skill):
    """Executes a trader's explicit sell against our open shares, then halts the
    entry path (returns skip). Self-gating: pass-through for non-sell signals.

    Idempotent (fingerprint-claimed) and RTH-gated; never auto-retries a failed
    sell (alerted for manual handling) — consistent with the no-auto-anything
    philosophy. Options legs are left held (shares-only v1)."""

    name = "SellFollower"

    def __init__(self, gateway, intent_store, exits_store, *,
                 slippage_cap_pct: float, fill_timeout: float, is_rth=None) -> None:
        self._gw = gateway
        self._intents = intent_store
        self._exits = exits_store
        self._cap = slippage_cap_pct
        self._timeout = fill_timeout
        self._is_rth = is_rth or (lambda: _in_rth(datetime.now(EASTERN)))

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("action") != "sell":
            return SkillResult(status="success")  # pass-through for entries

        if not self._is_rth():
            return SkillResult(status="skip", reason="sell_outside_rth")

        ticker = ctx.get("sell_ticker")
        channel = ctx.get("channel")
        fingerprint = ctx.get("message_fingerprint") or ctx.event_id

        positions = await self._intents.get_open_shares_positions(channel, ticker)
        remaining = []
        for p in positions:
            rem = await self._exits.remaining_qty(p["intent_id"])
            if rem > 0:
                remaining.append((p["intent_id"], rem))
        if not remaining:
            return SkillResult(status="skip", reason="no_open_position")

        # Claim AFTER confirming there is something to sell (so a no-position
        # event doesn't burn the claim), BEFORE placing any order.
        if not await self._exits.claim_sell_event(fingerprint, ctx.event_id):
            return SkillResult(status="skip", reason="sell_already_followed")

        agg = sum(r for _, r in remaining)
        scope = ctx.get("sell_scope", "full")
        if scope == "full":
            target = agg
        else:
            target = max(1, math.floor(agg * float(ctx.get("sell_fraction", 0.5))))

        total_sold = 0
        try:
            for intent_id, rem in remaining:        # oldest-first
                if target <= 0:
                    break
                alloc = min(rem, target)
                sold = await follow_sell_position(
                    gw=self._gw, exits_store=self._exits, fingerprint=fingerprint,
                    event_id=ctx.event_id, intent_id=intent_id, channel=channel,
                    ticker=ticker, qty=alloc, scope=scope,
                    slippage_cap_pct=self._cap, fill_timeout=self._timeout)
                total_sold += sold
                target -= sold
        except IBGatewayUnavailable as exc:
            if total_sold == 0:
                # Nothing sold -> release the claim so a repost can retry.
                await self._exits.release_sell_event(fingerprint)
                return SkillResult(status="skip",
                                   reason=f"sell_broker_unavailable:{exc}")
            return SkillResult(status="skip",
                               reason=f"sell_partial_broker_unavailable:{exc}")

        ctx.update({"sell_total_sold_qty": total_sold, "sell_ticker": ticker})
        if total_sold == 0:
            # Every lot zero-filled in RTH (limit didn't fill). Do NOT report
            # success — alert for manual handling (claim stays; no auto-retry).
            logger.warning("SellFollower: %s zero-fill, nothing sold", ticker)
            return SkillResult(status="skip", reason="sell_zero_fill",
                               updates={"sell_total_sold_qty": 0})
        logger.info("SellFollower: %s sold %d shares (%s)", ticker, total_sold, scope)
        return SkillResult(status="skip", reason="sell_followed",
                           updates={"sell_total_sold_qty": total_sold})
