from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import BrokerContractRef

logger = logging.getLogger(__name__)


class TickerValidator(Skill):
    name = "TickerValidator"

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        side = ctx.get("side", "")

        if not ticker:
            return SkillResult(status="fail", reason="ticker_validator: ticker missing from context")

        if side not in ("long", "short"):
            return SkillResult(
                status="skip",
                reason=f"ambiguous_signal: side='{side}' is not long or short",
            )

        ref = BrokerContractRef(
            symbol=ticker, sec_type="STK", exchange="SMART", currency="USD"
        )
        try:
            qualified = await self._gateway.qualify(ref)
            if not qualified.qualified:
                raise IBGatewayUnavailable(f"qualify returned unqualified ref for {ticker}")
        except IBGatewayUnavailable as exc:
            return SkillResult(
                status="skip",
                reason=f"ambiguous_signal: ticker '{ticker}' could not be validated: {exc}",
            )

        logger.info("TickerValidator: %s validated (side=%s)", ticker, side)
        return SkillResult(status="success")
