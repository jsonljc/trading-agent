from __future__ import annotations
import math
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)


class OrderSizer(Skill):
    name = "OrderSizer"

    def __init__(self, policy, gateway) -> None:
        self._policy = policy
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        try:
            account = await self._gateway.get_account_summary()
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        instrument_type = ctx.get("instrument_type", "option")
        conviction = ctx.get("conviction_bucket", "low")
        sp = self._policy.sizing_policy
        conviction_pct = sp.high_conviction_pct if conviction == "high" else sp.low_conviction_pct

        allocation = account.buying_power * conviction_pct

        if instrument_type == "option":
            candidates = ctx.get("option_candidates", [])
            selected_strike = ctx.get("selected_strike")
            matching = [c for c in candidates if c.strike == selected_strike]
            if not matching:
                return SkillResult(status="fail", reason="order_sizer: no matching candidate for selected strike")
            candidate = matching[0]
            ask = candidate.ask
            multiplier = candidate.multiplier
            cost_per_contract = ask * multiplier
            quantity = math.floor(allocation / cost_per_contract)
            notional = quantity * cost_per_contract
        else:
            ticker = ctx.get("ticker")
            try:
                ask = await self._gateway.get_quote(ticker)
            except IBGatewayUnavailable as exc:
                return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")
            quantity = math.floor(allocation / ask)
            notional = quantity * ask

        if quantity < 1:
            return SkillResult(
                status="fail",
                reason=f"insufficient_buying_power: allocation={allocation:.2f} insufficient for 1 unit at {ask}",
            )

        reason = (
            f"{conviction}_conviction {conviction_pct*100:.0f}% of "
            f"${account.buying_power:,.0f} buying_power"
        )
        logger.info("OrderSizer: qty=%d notional=%.2f (%s)", quantity, notional, reason)
        return SkillResult(status="success", updates={
            "quantity": quantity,
            "notional_estimate": notional,
            "sizing_reason": reason,
            "capped_by": None,
        })
