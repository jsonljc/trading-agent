from __future__ import annotations

import math
from typing import Awaitable, Callable, Optional

from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import (
    AccountSummary, BrokerContractRef, FillResult, FillStatus, PreparedOrder,
)


class _FakeTrade:
    __slots__ = ("order",)

    def __init__(self, order: PreparedOrder) -> None:
        self.order = order


class FakeGateway:
    """Deterministic broker stand-in. Serves the sell/trim path
    (qualify_equity/get_quote/place_order/wait_fill/cancel_order) and OrderSizer
    (get_account_summary). `fill_mode` controls how the NEXT wait_fill resolves.
    """

    def __init__(self, *, quote: float = 100.0, net_liquidation: float = 100_000.0,
                 buying_power: float = 100_000.0) -> None:
        self.quote = quote
        self.account = AccountSummary(
            net_liquidation=net_liquidation, buying_power=buying_power, currency="USD")
        self.fill_mode = "full"          # "full" | "partial" | "zero"
        self.partial_fraction = 0.5
        self.unavailable = False
        self.placed: list[PreparedOrder] = []
        self.cancels = 0
        # §3a hook: a one-shot async callback run INSIDE wait_fill, i.e. while a
        # sell order is placed-but-unrecorded. Used to inject a concurrent trim.
        self.on_wait_fill: Optional[Callable[[], Awaitable[None]]] = None

    async def qualify_equity(self, ticker: str) -> BrokerContractRef:
        return BrokerContractRef(symbol=ticker, sec_type="STK", exchange="SMART",
                                 currency="USD", qualified=True)

    async def get_quote(self, ticker: str) -> float:
        if self.unavailable:
            raise IBGatewayUnavailable("fake: unavailable")
        return self.quote

    async def get_account_summary(self) -> AccountSummary:
        if self.unavailable:
            raise IBGatewayUnavailable("fake: unavailable")
        return self.account

    async def place_order(self, contract, order: PreparedOrder, client_order_id: str):
        if self.unavailable:
            raise IBGatewayUnavailable("fake: unavailable")
        self.placed.append(order)
        return _FakeTrade(order)

    async def wait_fill(self, trade: _FakeTrade, timeout: float) -> FillResult:
        if self.on_wait_fill is not None:
            cb, self.on_wait_fill = self.on_wait_fill, None  # one-shot
            await cb()
        qty = trade.order.quantity
        if self.fill_mode == "full":
            filled, status = qty, FillStatus.FILLED
        elif self.fill_mode == "partial":
            filled = max(0, math.floor(qty * self.partial_fraction))
            status = FillStatus.TIMED_OUT_PENDING
        else:  # "zero"
            filled, status = 0, FillStatus.TIMED_OUT_PENDING
        return FillResult(
            status=status, broker_order_id="fake-oid", perm_id=1,
            submitted_qty=qty, filled_qty=filled, remaining_qty=qty - filled,
            avg_fill_price=(self.quote if filled > 0 else None),
            last_status=("Filled" if status == FillStatus.FILLED else "Submitted"),
            status_timestamp="2026-06-26T14:30:00+00:00")

    async def cancel_order(self, trade) -> bool:
        self.cancels += 1
        return True
