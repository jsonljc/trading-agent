from __future__ import annotations
from datetime import date, datetime
from agent.context import Context, SkillResult
from agent.skill import Skill
from skills.execution._options_leg import already_terminated, partial_or


class ContractSelector(Skill):
    name = "ContractSelector"

    def __init__(self, policy) -> None:
        self._policy = policy

    async def run(self, ctx: Context) -> SkillResult:
        if (r := already_terminated(ctx)):
            return r

        candidates = ctx.get("option_candidates", [])
        ip = self._policy.instrument_policy
        pg = self._policy.pricing_policy_guards
        # Prefer reference_price (captured at signal time by ReferencePriceCapture)
        # over spot_price for ITM/OTM determination. spot_price is unset in the
        # live chain; reference_price is the actual at-signal quote.
        spot = ctx.get("spot_price") or ctx.get("reference_price") or 0.0

        today = date.today()
        eligible = []
        for c in candidates:
            expiry_date = datetime.strptime(c.expiry, "%Y-%m-%d").date()
            days_to_expiry = (expiry_date - today).days
            if days_to_expiry < ip.min_expiry_days:
                continue
            if c.bid < pg.min_bid:
                continue
            if c.spread_pct > pg.max_spread_pct:
                continue
            eligible.append(c)

        if not eligible:
            return partial_or(ctx, "no_eligible_contract: no candidates pass filters", "fail")

        # closest_itm_call: largest strike below spot
        itm = [c for c in eligible if c.right == "C" and c.strike < spot]
        if not itm:
            # fallback: lowest strike above spot (nearest OTM)
            otm = sorted([c for c in eligible if c.right == "C"], key=lambda c: c.strike)
            if not otm:
                return partial_or(ctx, "no_eligible_contract: no call candidates", "fail")
            selected = otm[0]
        else:
            selected = max(itm, key=lambda c: c.strike)

        return SkillResult(status="success", updates={
            "selected_contract": selected.contract_ref,
            "selected_expiry": selected.expiry,
            "selected_strike": selected.strike,
            "instrument_type": "option",
        })
