from __future__ import annotations
from datetime import datetime, timezone
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution._pricing import marketable_limit

logger = logging.getLogger(__name__)


class SharesMarketSubmitter(Skill):
    name = "SharesMarketSubmitter"

    def __init__(self, gateway, intent_store, trim_store,
                 *, fill_timeout: float, trim_rungs: list[tuple[int, float, float]],
                 slippage_cap_pct: float = 0.01):
        self._gateway = gateway
        self._intents = intent_store
        self._trims = trim_store
        self._timeout = fill_timeout
        self._rungs = trim_rungs
        self._cap = slippage_cap_pct

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("side") == "short":
            return SkillResult(status="skip", reason="unsupported_short_signal")

        contract = ctx.get("selected_contract")
        qty = ctx.get("quantity")
        if not contract or not contract.qualified or not qty or qty < 1:
            return SkillResult(status="fail", reason="shares_submit: missing contract/qty")

        intent_id = ctx.get("intent_id")
        client_order_id = f"{ctx.trace_id}:shares:{ctx.event_id}"
        try:
            # Marketable limit off the LIVE ask (caps slippage; still fills at NBBO).
            ask = await self._gateway.get_quote(ctx.get("ticker"))
            limit = marketable_limit(ask, self._cap)
            order = PreparedOrder(action="BUY", quantity=qty, order_type="LMT",
                                  limit_price=limit, tif="DAY")
            trade = await self._gateway.place_order(contract, order, client_order_id)
            # Write-ahead BEFORE waiting for the fill: a crash mid-fill then
            # leaves a 'submitted'/'dispatched' row the reconciler can resolve
            # against IB's open orders on restart.
            await self._intents.update_execution_state(
                intent_id, "submitted", outbox_status="dispatched",
                broker_order_ref=str(trade.order.orderId),
                order_submitted_at=datetime.now(timezone.utc).isoformat(),
            )
            fill = await self._gateway.wait_fill(trade, timeout=self._timeout)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable:{exc}")

        if fill.status == FillStatus.REJECTED:
            # Distinct from a timeout: a hard broker rejection -> DLQ.
            await self._intents.update_execution_state(
                intent_id, "failed", outbox_status="failed",
                dlq_reason=f"broker_rejected:{fill.last_status}",
            )
            return SkillResult(status="fail",
                               reason=f"shares_rejected:{fill.last_status}")

        # Anything with filled_qty>0 is a real (possibly partial) position we own.
        if fill.filled_qty <= 0:
            await self._cancel_residual(trade, fill)
            await self._intents.update_execution_state(
                intent_id, "cancelled", outbox_status="cancelled",
                cancel_reason="fill_timeout",
                cancelled_at=datetime.now(timezone.utc).isoformat(),
            )
            return SkillResult(status="fail",
                               reason=f"shares_not_filled:{fill.last_status}")

        if fill.status != FillStatus.FILLED:
            # Partial fill: cancel the residual working order so it can't fill late
            # unattended, then arm trims on the qty we actually own.
            await self._cancel_residual(trade, fill)
            logger.warning("shares partial fill %s: %d/%d filled, residual cancelled",
                           client_order_id, fill.filled_qty, fill.submitted_qty)

        await self._intents.update_fill(
            intent_id, fill_price=fill.avg_fill_price or 0.0,
            fill_qty=fill.filled_qty, broker_order_ref=fill.broker_order_id,
        )
        if self._rungs:
            await self._trims.arm(
                intent_id, rungs=self._rungs,
                armed_at=datetime.now(timezone.utc).isoformat(),
            )
        return SkillResult(status="success", updates={
            "shares_intent_id": intent_id,
            "shares_fill_price": fill.avg_fill_price,
            "shares_fill_qty": fill.filled_qty,
        })

    async def _cancel_residual(self, trade, fill) -> None:
        """Best-effort cancel of a working order's remainder. Never mask a real
        fill — log and move on if the cancel fails."""
        if fill.status == FillStatus.FILLED:
            return
        try:
            await self._gateway.cancel_order(trade)
        except Exception:
            logger.exception("shares residual cancel failed (order may rest at IB)")
