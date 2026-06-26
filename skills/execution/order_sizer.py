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
            unit_cost = cand.ask * cand.multiplier   # premium x multiplier
            ask = cand.ask
        else:
            # Reuse the quote ReferencePriceCapture already fetched upstream
            # (ctx["reference_price"]) instead of re-issuing the same get_quote —
            # one fewer IB round-trip on the hot path. Fall back to a live fetch
            # only in compositions that ran no ReferencePriceCapture.
            ask = ctx.get("reference_price")
            if ask is None:
                ticker = ctx.get("ticker")
                try:
                    ask = await self._gateway.get_quote(ticker)
                except IBGatewayUnavailable as exc:
                    return partial_or(ctx, f"broker_unavailable: {exc}", "fail")
            unit_cost = ask

        quantity = math.floor(allocation / unit_cost)
        notional = quantity * unit_cost
        capped_by = None

        # --- Buying-power ceiling: never size an order past live broker buying
        # power. IB also reserves buying power as each concurrent order is
        # accepted, so re-reading it per order serialises simultaneous signals at
        # the broker (the root cause of "stack orders until IB rejects").
        buying_power = getattr(account, "buying_power", None)
        if buying_power is not None and notional > buying_power:
            quantity = math.floor(buying_power / unit_cost)
            notional = quantity * unit_cost
            capped_by = "buying_power"

        if quantity < 1:
            return partial_or(
                ctx,
                f"insufficient_buying_power: alloc={allocation:.2f} "
                f"bp={buying_power} < 1 unit at {unit_cost:.2f}",
                "skip")

        # --- Aggregate exposure ceiling: skip (do not partially deploy) if this
        # order would push total open deployed notional past the policy cap.
        # ExposureGuard stashes these keys upstream; they are absent only in
        # non-production compositions where the guard (which owns the fail-safe)
        # did not run, in which case the aggregate cap is inert and only the
        # buying-power clamp above applies.
        open_exposure = ctx.get("open_exposure")
        agg_cap = ctx.get("aggregate_notional_cap")
        if (open_exposure is not None and agg_cap is not None
                and open_exposure + notional > agg_cap):
            return partial_or(
                ctx,
                f"exposure_cap_exceeded: open={open_exposure:.0f} + "
                f"order={notional:.0f} > cap={agg_cap:.0f}",
                "skip")

        reason = (f"{instrument_type} pct={size_pct:.4f} of "
                  f"NetLiq=${account.net_liquidation:,.0f} × {self._margin_multiplier}")
        logger.info("OrderSizer: qty=%d notional=%.2f capped_by=%s (%s)",
                    quantity, notional, capped_by, reason)
        updates = {
            "quantity": quantity,
            "notional_estimate": notional,
            "sizing_reason": reason,
            "capped_by": capped_by,
        }
        if open_exposure is not None:
            # Running deployed-notional tally so a later leg (the options sleeve
            # after the shares fill) re-checks the aggregate cap including the
            # capital this leg just committed.
            updates["open_exposure"] = open_exposure + notional
        if instrument_type == "option":
            # Cached ask; OptionsMarketSubmitter prefers a fresh live ask but
            # falls back to this when the live quote is missing (delayed data).
            updates["option_ask"] = ask
        return SkillResult(status="success", updates=updates)
