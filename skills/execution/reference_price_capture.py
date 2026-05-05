from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable


class ReferencePriceCapture(Skill):
    name = "ReferencePriceCapture"

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        try:
            price = await self._gateway.get_quote(ticker)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail",
                               reason=f"reference_price_unavailable:{exc}")
        return SkillResult(status="success", updates={"reference_price": price})
