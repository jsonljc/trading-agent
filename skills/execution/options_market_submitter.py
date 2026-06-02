from __future__ import annotations
import uuid
from datetime import datetime, timezone
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution._options_leg import already_terminated, partial_or
from skills.execution._pricing import marketable_limit

logger = logging.getLogger(__name__)


class OptionsMarketSubmitter(Skill):
    name = "OptionsMarketSubmitter"

    def __init__(self, gateway, intent_store, *, fill_timeout: float,
                 slippage_cap_pct: float = 0.05):
        self._gateway = gateway
        self._intents = intent_store
        self._timeout = fill_timeout
        self._cap = slippage_cap_pct

    async def run(self, ctx: Context) -> SkillResult:
        if (r := already_terminated(ctx)):
            return r
        if ctx.get("side") == "short":
            return partial_or(ctx, "unsupported_short_signal", "skip")

        contract = ctx.get("selected_contract")
        qty = ctx.get("quantity")
        if not contract or not contract.qualified or not qty or qty < 1:
            # This is a chain-ordering invariant violation: ContractSelector
            # and OrderSizer should have either populated these or returned
            # a partial earlier. Hard-fail (don't swallow as partial) so the
            # bug surfaces; the orchestrator's on_fail still runs after the
            # shares leg has already filled, but that's the right signal.
            logger.error(
                "OptionsMarketSubmitter: missing contract/qty after sub-chain "
                "(contract=%s qualified=%s qty=%s) — chain-ordering bug",
                contract, getattr(contract, "qualified", None), qty,
            )
            return SkillResult(status="fail",
                               reason="options_submit: missing contract/qty")

        # Marketable limit off the LIVE option ask (sizing used the cached
        # chain-lookup ask). Fall back to that cached ask if the live quote is
        # missing; if neither is available we cannot price a limit.
        try:
            live_ask, _age = await self._gateway.get_option_ask(contract)
        except IBGatewayUnavailable as exc:
            return partial_or(ctx, f"broker_unavailable:{exc}", "fail")
        ask = live_ask if live_ask and live_ask > 0 else (ctx.get("option_ask") or 0.0)
        if ask <= 0:
            return partial_or(ctx, "option_no_ask: cannot price limit", "fail")
        limit = marketable_limit(ask, self._cap)

        order = PreparedOrder(action="BUY", quantity=qty, order_type="LMT",
                              limit_price=limit, tif="DAY")
        client_order_id = f"{ctx.trace_id}:options:{ctx.event_id}"
        try:
            trade = await self._gateway.place_order(contract, order, client_order_id)
            fill = await self._gateway.wait_fill(trade, timeout=self._timeout)
        except IBGatewayUnavailable as exc:
            return partial_or(ctx, f"broker_unavailable:{exc}", "fail")

        if fill.status == FillStatus.REJECTED:
            # Distinct from a timeout so it routes to the ORDER REJECTED alert.
            # (The options leg is written post-fill, so there is no intent row to
            # mark 'failed'/dlq here — the shares leg is the DLQ producer.)
            return partial_or(ctx, f"options_rejected:{fill.last_status}", "fail")

        if fill.filled_qty <= 0:
            await self._cancel_residual(trade, fill)
            return partial_or(ctx, f"options_not_filled:{fill.last_status}", "fail")

        if fill.status != FillStatus.FILLED:
            # Partial fill: cancel the residual, record the contracts we own.
            await self._cancel_residual(trade, fill)
            logger.warning("options partial fill %s: %d/%d filled, residual cancelled",
                           client_order_id, fill.filled_qty, fill.submitted_qty)

        options_intent_id = str(uuid.uuid4())
        await self._intents.write(
            intent_id=options_intent_id,
            event_id=ctx.event_id,
            channel=ctx.get("channel"),
            ticker=ctx.get("ticker"),
            side=ctx.get("side"),
            instrument_type="option",
            parent_intent_id=ctx.get("shares_intent_id"),
            expiry=ctx.get("selected_expiry"),
            strike=ctx.get("selected_strike"),
            right="C",
            conviction=ctx.get("bucket"),
            fill_price=fill.avg_fill_price,
            fill_qty=fill.filled_qty,
            execution_state="filled",
            signal_received_at=ctx.get("received_at",
                                       datetime.now(timezone.utc).isoformat()),
            broker_order_ref=fill.broker_order_id,
        )
        return SkillResult(status="success", updates={
            "options_intent_id": options_intent_id,
            "options_fill_price": fill.avg_fill_price,
            "options_fill_qty": fill.filled_qty,
        })

    async def _cancel_residual(self, trade, fill) -> None:
        """Best-effort cancel of the working order's remainder."""
        if fill.status == FillStatus.FILLED:
            return
        try:
            await self._gateway.cancel_order(trade)
        except Exception:
            logger.exception("options residual cancel failed (order may rest at IB)")
