from __future__ import annotations
import uuid
from datetime import datetime, timezone
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution._options_leg import already_terminated, partial_or

logger = logging.getLogger(__name__)


class OptionsMarketSubmitter(Skill):
    name = "OptionsMarketSubmitter"

    def __init__(self, gateway, intent_store, *, fill_timeout: float):
        self._gateway = gateway
        self._intents = intent_store
        self._timeout = fill_timeout

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

        order = PreparedOrder(action="BUY", quantity=qty, order_type="MKT",
                              limit_price=None, tif="DAY")
        client_order_id = f"{ctx.trace_id}:options:{ctx.event_id}"
        try:
            trade = await self._gateway.place_order(contract, order, client_order_id)
            fill = await self._gateway.wait_fill(trade, timeout=self._timeout)
        except IBGatewayUnavailable as exc:
            return partial_or(ctx, f"broker_unavailable:{exc}", "fail")

        if fill.status != FillStatus.FILLED:
            return partial_or(ctx, f"options_not_filled:{fill.last_status}", "fail")

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
