from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel
from infra.ib.gateway import IBGatewayUnavailable
from skills.risk.exposure import open_deployed_notional

logger = logging.getLogger(__name__)


class ExposureGuard(Skill):
    """Pre-trade spending / exposure ceilings. Default-deny: any input that
    cannot be read skips the entry rather than risk over-deploying real capital.

    1. max_equity_price -- reject an entry whose underlying per-share reference
       price exceeds policy.execution.max_equity_price.
    2. aggregate exposure cap -- if total open deployed notional already meets
       net_liquidation x max_deployed_pct there is no room, so skip. Otherwise
       hand (open_exposure, aggregate_notional_cap) to OrderSizer, which clamps
       or skips the order so the running total stays under the cap AND under
       live broker buying power.

    The per-order buying-power clamp itself lives in OrderSizer (it is the only
    skill that knows the exact per-leg notional and re-reads live buying power).
    """
    name = "ExposureGuard"

    def __init__(self, policy: PolicyModel, gateway, trade_intent_store,
                 *, exposure_fn=open_deployed_notional) -> None:
        self._policy = policy
        self._gateway = gateway
        self._store = trade_intent_store
        self._exposure_fn = exposure_fn

    async def run(self, ctx: Context) -> SkillResult:
        execp = self._policy.execution

        # 1) Per-share reference-price ceiling (the underlying equity spot,
        #    captured by ReferencePriceCapture immediately upstream).
        ref = ctx.get("reference_price")
        if ref is not None and ref > execp.max_equity_price:
            return SkillResult(
                status="skip",
                reason=(f"above_max_equity_price: ref={ref:.2f} > "
                        f"max={execp.max_equity_price:.2f}"))

        # 2) Account equity for the aggregate cap. Broker down -> fail safe.
        try:
            account = await self._gateway.get_account_summary()
        except IBGatewayUnavailable as exc:
            return SkillResult(status="skip",
                               reason=f"exposure_data_unavailable: broker {exc}")

        # 3) Current open deployed exposure. Any storage error -> fail safe.
        try:
            open_notional = await self._exposure_fn(self._store)
        except Exception as exc:  # noqa: BLE001 - default-deny on any DB error
            logger.exception(
                "ExposureGuard: open-exposure query failed -- skipping entry")
            return SkillResult(status="skip",
                               reason=f"exposure_data_unavailable: {exc}")

        agg_cap = account.net_liquidation * execp.max_deployed_pct
        if open_notional >= agg_cap:
            return SkillResult(
                status="skip",
                reason=(f"exposure_cap_exceeded: open={open_notional:,.0f} >= "
                        f"cap={agg_cap:,.0f} "
                        f"(netliq={account.net_liquidation:,.0f} "
                        f"x {execp.max_deployed_pct})"))

        logger.info("ExposureGuard: open=%.0f cap=%.0f headroom=%.0f",
                    open_notional, agg_cap, agg_cap - open_notional)
        return SkillResult(status="success", updates={
            "open_exposure": open_notional,
            "aggregate_notional_cap": agg_cap,
        })
