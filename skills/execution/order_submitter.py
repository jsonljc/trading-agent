from __future__ import annotations
import uuid
import json
import logging
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import FillStatus, PreparedOrder

logger = logging.getLogger(__name__)


class OrderSubmitter(Skill):
    name = "OrderSubmitter"

    def __init__(self, gateway, execution_store) -> None:
        self._gateway = gateway
        self._store = execution_store

    async def run(self, ctx: Context) -> SkillResult:
        signal_id = ctx.get("signal_id", ctx.event_id)
        instrument_type = ctx.get("instrument_type", "option")
        contract_ref = ctx.get("selected_contract")
        quantity = ctx.get("quantity")
        limit_price = ctx.get("limit_price")
        ticker = ctx.get("ticker")

        # Equity contracts must be qualified here; options arrive pre-qualified
        if instrument_type == "equity" and not contract_ref.qualified:
            contract_ref = await self._gateway.qualify(contract_ref)

        if not contract_ref.qualified:
            return SkillResult(status="fail", reason="order_submitter: contract not qualified")

        idempotency_key = f"{ctx.trace_id}:OrderSubmitter:{signal_id}"
        execution_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Write execution row BEFORE calling place_order (write-before-submit invariant)
        await self._store.insert_execution({
            "id": execution_id,
            "signal_id": signal_id,
            "trace_id": ctx.trace_id,
            "instrument_type": instrument_type,
            "ticker": ticker,
            "contract_ref_json": _ref_json(contract_ref),
            "quantity": quantity,
            "notional_estimate": ctx.get("notional_estimate"),
            "limit_price": limit_price,
            "sizing_reason": ctx.get("sizing_reason"),
            "capped_by": ctx.get("capped_by"),
            "broker_order_id": None,
            "perm_id": None,
            "status": FillStatus.SUBMITTED_UNFILLED.value,
            "filled_qty": 0,
            "avg_fill_price": None,
            "idempotency_key": idempotency_key,
            "submitted_at": now,
            "filled_at": None,
            "last_known_at": now,
        })

        order = PreparedOrder(
            action=ctx.get("action", "BUY"),
            quantity=quantity,
            order_type="LMT",
            limit_price=limit_price,
            tif="DAY",
        )

        try:
            trade = await self._gateway.place_order(contract_ref, order, idempotency_key)
        except IBGatewayUnavailable as exc:
            return SkillResult(status="fail", reason=f"broker_unavailable: {exc}")

        broker_order_id = str(trade.order.orderId)
        await self._store.update_execution_status(
            execution_id=execution_id,
            status=FillStatus.SUBMITTED_UNFILLED,
            broker_order_id=broker_order_id,
        )

        logger.info("OrderSubmitter: submitted %s qty=%d @ %.2f key=%s",
                    ticker, quantity, limit_price, idempotency_key)
        return SkillResult(status="success", updates={
            "broker_order_id": broker_order_id,
            "idempotency_key": idempotency_key,
            "execution_id": execution_id,
            "_trade": trade,
        })


def _ref_json(ref) -> str:
    return json.dumps({
        "symbol": ref.symbol, "sec_type": ref.sec_type,
        "exchange": ref.exchange, "currency": ref.currency,
        "con_id": ref.con_id, "expiry": ref.expiry,
        "strike": ref.strike, "right": ref.right,
        "multiplier": ref.multiplier, "qualified": ref.qualified,
    })
