from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class OrderPricer(Skill):
    name = "OrderPricer"

    def __init__(self, policy) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        instrument_type = ctx.get("instrument_type", "option")
        pp = self._policy.pricing_policy
        pg = self._policy.pricing_policy_guards

        if instrument_type == "option":
            candidates = ctx.get("option_candidates", [])
            selected_strike = ctx.get("selected_strike")
            matching = [c for c in candidates if c.strike == selected_strike]
            if not matching:
                return SkillResult(status="fail", reason="order_pricer: no matching candidate")
            c = matching[0]
            if c.bid < pg.min_bid:
                return SkillResult(status="fail", reason=f"order_pricer: bid {c.bid} below min_bid {pg.min_bid}")
            if c.spread_pct > pg.max_spread_pct:
                return SkillResult(status="fail", reason=f"order_pricer: spread {c.spread_pct:.2%} exceeds max")
            mid = (c.bid + c.ask) / 2
            limit_price = round(mid + (c.ask - mid) * pp.option_spread_fraction, 2)
        else:
            ask = ctx.get("_equity_ask")
            if ask is None or ask <= 0:
                return SkillResult(status="fail", reason="order_pricer: equity ask missing or zero")
            max_price = self._policy.execution.max_equity_price
            if ask > max_price:
                return SkillResult(status="fail",
                                   reason=f"order_pricer: ask {ask} exceeds max_equity_price {max_price}")
            limit_price = round(ask * (1 + pp.stock_buffer_pct), 2)

        logger.info("OrderPricer: limit_price=%.2f type=%s", limit_price, instrument_type)
        return SkillResult(status="success", updates={
            "limit_price": limit_price,
            "order_type": "LMT",
        })
