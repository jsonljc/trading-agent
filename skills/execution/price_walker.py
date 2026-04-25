from __future__ import annotations
import asyncio
import logging
import math
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import PreparedOrder

logger = logging.getLogger(__name__)

_STALE_QUOTE_THRESHOLD_S = 5.0
_CANCEL_WAIT_TIMEOUT_S = 5.0


def _round_up_to_tick(price: float, tick: float = 0.05) -> float:
    return math.ceil(price / tick) * tick


class PriceWalker(Skill):
    name = "PriceWalker"

    def __init__(self, policy, gateway, trade_intent_store) -> None:
        self._policy = policy
        self._gateway = gateway
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        ep = self._policy.execution
        intent_id = ctx.get("intent_id")
        contract_ref = ctx.get("selected_contract")
        quantity = ctx.get("quantity")
        initial_reference_ask = ctx.get("initial_reference_ask")

        if not all([contract_ref, quantity, initial_reference_ask]):
            return SkillResult(status="fail",
                               reason="price_walker: missing contract_ref, quantity, or initial_reference_ask")

        profile_name = ctx.get("walk_profile") or ep.walk_profile
        step_buffers: list[float] = ep.walk_profiles.get(profile_name, ep.walk_profiles["aggressive_fast"])
        max_chase_price = initial_reference_ask * (1.0 + ep.max_chase_pct)
        reprice_interval_s = ep.reprice_interval_ms / 1000.0

        order_submitted_at = None
        attempt_count = 0
        trade = None
        last_limit_price = None

        for step_idx, step_buffer in enumerate(step_buffers):
            try:
                ask, age_s = await self._gateway.get_option_ask(contract_ref)
            except IBGatewayUnavailable as exc:
                await self._mark_failed(intent_id, f"broker_unavailable: {exc}")
                return SkillResult(status="fail", reason=f"price_walker broker error: {exc}")

            if age_s > _STALE_QUOTE_THRESHOLD_S:
                reason = "stale_quote"
                await self._mark_cancelled(intent_id, reason)
                return SkillResult(status="skip", reason=f"cancelled_unfilled: {reason}",
                                   updates=self._terminal_updates(attempt_count, last_limit_price))

            raw_limit = ask * (1.0 + step_buffer)
            if raw_limit > max_chase_price:
                reason = "price_exceeded_cap"
                await self._mark_cancelled(intent_id, reason)
                return SkillResult(status="skip", reason=f"cancelled_unfilled: {reason}",
                                   updates=self._terminal_updates(attempt_count, last_limit_price))

            limit_price = _round_up_to_tick(min(raw_limit, max_chase_price))
            last_limit_price = limit_price

            order = PreparedOrder(
                action=ctx.get("action", "BUY"),
                quantity=quantity,
                order_type="LMT",
                limit_price=limit_price,
                tif="DAY",
            )
            idempotency_key = f"{ctx.trace_id}:PriceWalker:{ctx.event_id}:step{step_idx}"
            submitted_at = datetime.now(timezone.utc).isoformat()

            if order_submitted_at is None and intent_id:
                await self._store.update_outbox_status(intent_id, "pending")

            try:
                trade = await self._gateway.place_order(contract_ref, order, idempotency_key)
            except IBGatewayUnavailable as exc:
                await self._mark_failed(intent_id, f"broker_unavailable: {exc}")
                return SkillResult(status="fail", reason=f"price_walker broker error: {exc}")

            ack_at = datetime.now(timezone.utc).isoformat()
            attempt_count += 1
            if order_submitted_at is None:
                order_submitted_at = submitted_at
                if intent_id:
                    await self._store.update_execution_state(
                        intent_id,
                        execution_state="submitted",
                        outbox_status="dispatched",
                        order_submitted_at=order_submitted_at,
                        order_ack_at=ack_at,
                        initial_order_limit=limit_price,
                        broker_order_ref=str(trade.order.orderId),
                        walk_profile=profile_name,
                        max_chase_pct=ep.max_chase_pct,
                        max_chase_price=max_chase_price,
                        initial_reference_ask=initial_reference_ask,
                    )

            deadline = asyncio.get_event_loop().time() + reprice_interval_s
            filled = False
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.05)
                status = trade.orderStatus.status
                if status == "Filled":
                    filled = True
                    break
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    break

            if filled:
                fill_price = float(trade.orderStatus.avgFillPrice or limit_price)
                filled_at = datetime.now(timezone.utc).isoformat()
                if intent_id:
                    await self._store.update_execution_state(
                        intent_id,
                        execution_state="filled",
                        outbox_status="confirmed",
                        fill_price=fill_price,
                        filled_at=filled_at,
                        order_attempt_count=attempt_count,
                        last_limit_price=limit_price,
                    )
                logger.info("PriceWalker: filled %s qty=%d @ %.2f (step %d)",
                            ctx.get("ticker"), quantity, fill_price, step_idx)
                return SkillResult(status="success", updates={
                    "fill_status": "filled",
                    "fill_price": fill_price,
                    "filled_qty": int(trade.orderStatus.filled),
                    "avg_fill_price": fill_price,
                    "order_attempt_count": attempt_count,
                    "last_limit_price": limit_price,
                })

            if step_idx < len(step_buffers) - 1:
                await self._gateway.cancel_order(trade, timeout=_CANCEL_WAIT_TIMEOUT_S)

        reason = "walk_exhausted"
        await self._mark_cancelled(intent_id, reason)
        return SkillResult(status="skip", reason=f"cancelled_unfilled: {reason}",
                           updates=self._terminal_updates(attempt_count, last_limit_price))

    async def _mark_cancelled(self, intent_id: str | None, cancel_reason: str) -> None:
        if intent_id:
            await self._store.update_execution_state(
                intent_id,
                execution_state="cancelled_unfilled",
                cancel_reason=cancel_reason,
                cancelled_at=datetime.now(timezone.utc).isoformat(),
            )

    async def _mark_failed(self, intent_id: str | None, dlq_reason: str) -> None:
        if intent_id:
            await self._store.update_execution_state(
                intent_id,
                execution_state="failed",
                dlq_reason=dlq_reason,
            )

    def _terminal_updates(self, attempt_count: int, last_limit_price: float | None) -> dict:
        return {
            "fill_status": "cancelled_unfilled",
            "order_attempt_count": attempt_count,
            "last_limit_price": last_limit_price,
        }
