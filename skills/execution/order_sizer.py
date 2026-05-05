from __future__ import annotations
import math
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution._options_leg import already_terminated, partial_or

logger = logging.getLogger(__name__)


class OrderSizer(Skill):
    name = "OrderSizer"

    def __init__(self, gateway, *, margin_multiplier: float = 2.0) -> None:
        self._gateway = gateway
        self._margin_multiplier = margin_multiplier

    async def run(self, ctx: Context) -> SkillResult:
        if (r := already_terminated(ctx)):
            return r
        instrument_type = ctx.get("instrument_type", "option")
        size_pct = (ctx.get("shares_pct") if instrument_type == "equity"
                    else ctx.get("options_pct"))
        if size_pct is None or size_pct <= 0:
            return partial_or(ctx,
                              f"order_sizer: pct missing for {instrument_type}", "fail")

        try:
            account = await self._gateway.get_account_summary()
        except IBGatewayUnavailable as exc:
            return partial_or(ctx, f"broker_unavailable: {exc}", "fail")

        sizing_base = account.net_liquidation * self._margin_multiplier
        allocation = sizing_base * size_pct

        if instrument_type == "option":
            candidates = ctx.get("option_candidates", [])
            selected_strike = ctx.get("selected_strike")
            matching = [c for c in candidates if c.strike == selected_strike]
            if not matching:
                # Invariant violation: ContractSelector picked a strike not in
                # the candidate set. Hard-fail rather than swallow as partial.
                logger.error(
                    "OrderSizer: selected_strike=%s missing from %d candidates "
                    "— ContractSelector / ChainLookup desync",
                    selected_strike, len(candidates),
                )
                return SkillResult(status="fail",
                                   reason="order_sizer: no matching option candidate")
            cand = matching[0]
            cost_per_contract = cand.ask * cand.multiplier
            quantity = math.floor(allocation / cost_per_contract)
            notional = quantity * cost_per_contract
            ask = cand.ask
        else:
            ticker = ctx.get("ticker")
            try:
                ask = await self._gateway.get_quote(ticker)
            except IBGatewayUnavailable as exc:
                return partial_or(ctx, f"broker_unavailable: {exc}", "fail")
            quantity = math.floor(allocation / ask)
            notional = quantity * ask

        if quantity < 1:
            return partial_or(ctx,
                              f"insufficient_buying_power: alloc={allocation:.2f} < 1 unit at {ask}",
                              "fail")

        reason = (f"{instrument_type} pct={size_pct:.4f} of "
                  f"NetLiq=${account.net_liquidation:,.0f} × {self._margin_multiplier}")
        logger.info("OrderSizer: qty=%d notional=%.2f (%s)", quantity, notional, reason)
        return SkillResult(status="success", updates={
            "quantity": quantity,
            "notional_estimate": notional,
            "sizing_reason": reason,
            "capped_by": None,
        })
