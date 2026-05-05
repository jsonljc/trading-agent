from __future__ import annotations
import uuid
from datetime import datetime, timezone
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class OptionsMarketSubmitter(Skill):
    name = "OptionsMarketSubmitter"

    def __init__(self, gateway, intent_store, *, fill_timeout: float):
        self._gateway = gateway
        self._intents = intent_store
        self._timeout = fill_timeout

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("side") == "short":
            return SkillResult(status="skip", reason="unsupported_short_signal")

        contract = ctx.get("selected_contract")
        qty = ctx.get("quantity")
        if not contract or not contract.qualified or not qty or qty < 1:
            return SkillResult(status="fail", reason="options_submit: missing contract/qty")

        order = PreparedOrder(action="BUY", quantity=qty, order_type="MKT",
                              limit_price=None, tif="DAY")
        client_order_id = f"{ctx.get('trace_id')}:options:{ctx.get('event_id')}"
        try:
            trade = await self._gateway.place_order(contract, order, client_order_id)
            fill = await self._gateway.wait_fill(trade, timeout=self._timeout)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable:{exc}")

        if fill.status != FillStatus.FILLED:
            return SkillResult(status="fail",
                               reason=f"options_not_filled:{fill.last_status}")

        options_intent_id = str(uuid.uuid4())
        await self._intents.write(
            intent_id=options_intent_id,
            event_id=ctx.get("event_id"),
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
            signal_received_at=ctx.get("signal_received_at"),
        )
        return SkillResult(status="success", updates={
            "options_intent_id": options_intent_id,
            "options_fill_price": fill.avg_fill_price,
            "options_fill_qty": fill.filled_qty,
        })
