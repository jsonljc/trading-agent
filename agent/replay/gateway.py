"""ReplayGateway — a deterministic, no-op-broker gateway for the replay harness.

Implements every method the live skill chain calls (qualify_equity / qualify /
get_quote / get_account_summary / get_chain / place_order / wait_fill /
cancel_order). It NEVER touches the network or a real IB Gateway: quotes and
net-liq are fixed/configurable, options chains are empty (the options sub-chain
gracefully no-ops), and orders are recorded into `placed_orders` then "filled"
deterministically at the order's limit (or the quote for MKT).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

from infra.ib.models import (
    BrokerContractRef, AccountSummary, PreparedOrder, FillResult, FillStatus,
)


@dataclass
class _ReplayTrade:
    """Fake broker trade handle. Carries the order so wait_fill can fill it
    deterministically without any broker round-trip."""
    contract_ref: BrokerContractRef
    order: PreparedOrder
    client_order_id: str
    broker_order_id: str


class ReplayGateway:
    def __init__(
        self,
        *,
        quote: float = 100.0,
        # Per-ticker quote overrides. The harness path (runner.py) never passes
        # this — it uses the single `quote` — but it's kept as a tested,
        # intentional extension point for per-ticker replay scenarios.
        quotes: dict[str, float] | None = None,
        net_liq: float = 100_000.0,
    ) -> None:
        self._quote = quote
        self._quotes = dict(quotes or {})
        self._net_liq = net_liq
        # Recorded would-be orders (the whole point of the harness).
        self.placed_orders: list[dict] = []
        self._order_seq = 0

    # --- read paths -----------------------------------------------------
    async def qualify_equity(self, ticker: str) -> BrokerContractRef:
        return BrokerContractRef(
            symbol=ticker, sec_type="STK", exchange="SMART",
            currency="USD", con_id=1, qualified=True,
        )

    async def qualify(self, contract_ref: BrokerContractRef) -> BrokerContractRef:
        contract_ref.qualified = True
        if contract_ref.con_id is None:
            contract_ref.con_id = 1
        return contract_ref

    async def get_quote(self, ticker: str) -> float:
        return self._quotes.get(ticker, self._quote)

    async def get_account_summary(self) -> AccountSummary:
        return AccountSummary(
            buying_power=self._net_liq,
            net_liquidation=self._net_liq,
            currency="USD",
        )

    async def get_chain(self, ticker: str, spot_price: float | None = None) -> list:
        # Empty chain => the options sub-chain gracefully no-ops (ChainLookup
        # records 0 candidates; downstream options skills terminate as a
        # partial that does not fail the run). Shares are the validated path.
        return []

    # --- write paths (recorded, never real) -----------------------------
    async def place_order(
        self,
        contract_ref: BrokerContractRef,
        order: PreparedOrder,
        client_order_id: str,
    ):
        self._order_seq += 1
        broker_order_id = f"replay-{self._order_seq}"
        self.placed_orders.append({
            "client_order_id": client_order_id,
            "broker_order_id": broker_order_id,
            "action": order.action,
            "quantity": order.quantity,
            "order_type": order.order_type,
            "limit_price": order.limit_price,
            "tif": order.tif,
            "instrument": contract_ref.symbol,
            "sec_type": contract_ref.sec_type,
        })
        return _ReplayTrade(
            contract_ref=contract_ref, order=order,
            client_order_id=client_order_id, broker_order_id=broker_order_id,
        )

    async def wait_fill(self, trade, timeout: float) -> FillResult:
        order: PreparedOrder = trade.order
        # Deterministic full fill at the limit (or the quote for a market order).
        if order.order_type == "MKT" or order.limit_price is None:
            price = await self.get_quote(trade.contract_ref.symbol)
        else:
            price = order.limit_price
        qty = int(order.quantity)
        return FillResult(
            status=FillStatus.FILLED,
            broker_order_id=trade.broker_order_id,
            perm_id=None,
            submitted_qty=qty,
            filled_qty=qty,
            remaining_qty=0,
            avg_fill_price=float(price),
            last_status="Filled",
            status_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def cancel_order(self, trade, timeout: float = 5.0) -> bool:
        return True
