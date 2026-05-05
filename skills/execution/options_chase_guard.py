from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class OptionsChaseGuard(Skill):
    name = "OptionsChaseGuard"

    def __init__(self, gateway, *, threshold_pct: float) -> None:
        self._gateway = gateway
        self._threshold = threshold_pct

    async def run(self, ctx: Context) -> SkillResult:
        ref = ctx.get("reference_price")
        if ref is None or ref <= 0:
            return SkillResult(status="skip", reason="options_chase_skip:no_reference")
        ticker = ctx.get("ticker")
        try:
            current = await self._gateway.get_quote(ticker)
        except IBGatewayUnavailable as exc:
            logger.warning("OptionsChaseGuard: quote failed (%s); skipping options", exc)
            return SkillResult(status="skip",
                               reason=f"options_chase_skip:quote_unavailable:{exc}")
        ratio = current / ref
        if ratio > 1.0 + self._threshold:
            return SkillResult(status="skip",
                               reason=f"options_chase_skip: current={current} > ref={ref}×{1+self._threshold}")
        return SkillResult(status="success", updates={"options_current_price": current})
