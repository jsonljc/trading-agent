from __future__ import annotations
from datetime import datetime, timezone
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.models import PreparedOrder, FillStatus
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class SharesMarketSubmitter(Skill):
    name = "SharesMarketSubmitter"

    def __init__(self, gateway, intent_store, trim_store,
                 *, fill_timeout: float, trim_rungs: list[tuple[int, float, float]]):
        self._gateway = gateway
        self._intents = intent_store
        self._trims = trim_store
        self._timeout = fill_timeout
        self._rungs = trim_rungs

    async def run(self, ctx: Context) -> SkillResult:
        if ctx.get("side") == "short":
            return SkillResult(status="skip", reason="unsupported_short_signal")

        contract = ctx.get("selected_contract")
        qty = ctx.get("quantity")
        if not contract or not contract.qualified or not qty or qty < 1:
            return SkillResult(status="fail", reason="shares_submit: missing contract/qty")

        order = PreparedOrder(action="BUY", quantity=qty, order_type="MKT",
                              limit_price=None, tif="DAY")
        client_order_id = f"{ctx.get('trace_id')}:shares:{ctx.get('event_id')}"
        try:
            trade = await self._gateway.place_order(contract, order, client_order_id)
            fill = await self._gateway.wait_fill(trade, timeout=self._timeout)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable:{exc}")

        if fill.status != FillStatus.FILLED:
            return SkillResult(status="fail",
                               reason=f"shares_not_filled:{fill.last_status}")

        intent_id = ctx.get("intent_id")
        await self._intents.update_fill(
            intent_id, fill_price=fill.avg_fill_price or 0.0,
            fill_qty=fill.filled_qty,
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
