from __future__ import annotations
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable


class EquityContractBuilder(Skill):
    name = "EquityContractBuilder"

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        if not ticker:
            return SkillResult(status="fail", reason="equity_contract_builder: ticker missing")
        try:
            ref = await self._gateway.qualify_equity(ticker)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable:{exc}")
        return SkillResult(status="success", updates={
            "selected_contract": ref,
            "selected_expiry": None,
            "selected_strike": None,
            "instrument_type": "equity",
        })
