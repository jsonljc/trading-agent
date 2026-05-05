from __future__ import annotations
import json
import uuid
import logging
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from skills.execution._options_leg import already_terminated, partial_or

logger = logging.getLogger(__name__)


class ChainLookup(Skill):
    name = "ChainLookup"

    def __init__(self, gateway, conn) -> None:
        self._gateway = gateway
        self._conn = conn

    async def run(self, ctx: Context) -> SkillResult:
        if (r := already_terminated(ctx)):
            return r
        ticker = ctx.get("ticker")
        signal_id = ctx.get("signal_id", ctx.event_id)
        try:
            candidates = await self._gateway.get_chain(ticker)
        except IBGatewayUnavailable as exc:
            return partial_or(ctx, f"broker_unavailable: {exc}", "fail")

        now = datetime.now(timezone.utc).isoformat()
        for c in candidates:
            await self._conn.execute(
                """INSERT OR IGNORE INTO option_candidates
                   (id, trace_id, signal_id, symbol, expiry, strike, right,
                    bid, ask, mid, spread_pct, open_interest, volume, multiplier,
                    contract_ref_json, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), ctx.trace_id, signal_id,
                    c.symbol, c.expiry, c.strike, c.right,
                    c.bid, c.ask, c.mid, c.spread_pct,
                    c.open_interest, c.volume, c.multiplier,
                    json.dumps(_ref_to_dict(c.contract_ref)), now,
                ),
            )
        await self._conn.commit()
        logger.info("ChainLookup: %d candidates for %s", len(candidates), ticker)
        return SkillResult(status="success", updates={"option_candidates": candidates})


def _ref_to_dict(ref) -> dict:
    return {
        "symbol": ref.symbol, "sec_type": ref.sec_type,
        "exchange": ref.exchange, "currency": ref.currency,
        "con_id": ref.con_id, "expiry": ref.expiry, "strike": ref.strike,
        "right": ref.right, "multiplier": ref.multiplier,
        "local_symbol": ref.local_symbol, "trading_class": ref.trading_class,
        "qualified": ref.qualified,
    }
