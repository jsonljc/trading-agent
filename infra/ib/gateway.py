from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from infra.ib.models import (
    BrokerContractRef, OptionCandidate, AccountSummary,
    PreparedOrder, FillResult, FillStatus,
)

logger = logging.getLogger(__name__)


class IBGatewayUnavailable(Exception):
    pass


class LiveTradingBlocked(Exception):
    pass


@dataclass
class _CircuitBreaker:
    threshold: int = 3
    probe_interval: float = 30.0
    _failure_count: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.probe_interval:
            return False  # half-open: allow probe
        return True

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self.threshold:
            self._opened_at = time.monotonic()

    def _record_success(self) -> None:
        self._failure_count = 0
        self._opened_at = None

    def check(self) -> None:
        if self.is_open():
            raise IBGatewayUnavailable("circuit open")


class IBGateway:
    def __init__(self, policy) -> None:
        self._policy = policy
        self._ib = None
        self._connected = False
        self._account_id: str | None = None
        self._read_breaker = _CircuitBreaker()
        self._write_breaker = _CircuitBreaker()

    async def connect(self) -> None:
        from ib_insync import IB
        self._ib = IB()
        p = self._policy.ib_gateway
        await self._ib.connectAsync(p.host, p.port, clientId=p.client_id)
        # Enable delayed market data (free tier fallback)
        self._ib.reqMarketDataType(3)
        accounts = self._ib.managedAccounts()
        self._account_id = accounts[0] if accounts else None
        self._connected = True
        logger.info("Connected to IB Gateway. Account: %s", self._account_id)

    async def disconnect(self) -> None:
        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False

    async def qualify(self, contract_ref: BrokerContractRef) -> BrokerContractRef:
        self._read_breaker.check()
        try:
            ib_contract = _to_ib_contract(contract_ref)
            qualified = await self._ib.qualifyContractsAsync(ib_contract)
            if not qualified:
                raise IBGatewayUnavailable("qualification returned empty")
            result = _from_ib_contract(qualified[0])
            result.qualified = True
            self._read_breaker._record_success()
            return result
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"qualify failed: {exc}") from exc

    async def get_chain(self, ticker: str, spot_price: float | None = None) -> list[OptionCandidate]:
        self._read_breaker.check()
        try:
            from ib_insync import Stock, Option
            from datetime import date, timedelta

            stock = Stock(ticker, "SMART", "USD")
            if spot_price is not None:
                qualified_stocks = await self._ib.qualifyContractsAsync(stock)
                spot = spot_price
            else:
                qualified_stocks, spot = await asyncio.gather(
                    self._ib.qualifyContractsAsync(stock),
                    self._fetch_spot(ticker),
                )
            if not qualified_stocks:
                self._read_breaker._record_success()
                return []
            underlying_con_id = qualified_stocks[0].conId

            chains = await self._ib.reqSecDefOptParamsAsync(ticker, "", "STK", underlying_con_id)
            if not chains:
                self._read_breaker._record_success()
                return []
            chain = chains[0]

            min_expiry = self._policy.instrument_policy.min_expiry_days
            cutoff = date.today() + timedelta(days=min_expiry)
            valid_expiries = [
                e for e in chain.expirations
                if date(int(e[:4]), int(e[4:6]), int(e[6:])) >= cutoff
            ]

            all_strikes = sorted(chain.strikes)
            itm = [s for s in all_strikes if s <= spot][-3:]
            otm = [s for s in all_strikes if s > spot][:2]
            selected_strikes = set(itm + otm)

            pre_filtered = [
                (expiry, strike, "C")
                for expiry in valid_expiries
                for strike in selected_strikes
            ]

            if not pre_filtered:
                self._read_breaker._record_success()
                return []

            async def _qualify_and_quote(expiry: str, strike: float, right: str):
                opt = Option(ticker, expiry, strike, right, "SMART")
                try:
                    qualified = await self._ib.qualifyContractsAsync(opt)
                    if not qualified:
                        return None
                    q = qualified[0]
                    tickers = await self._ib.reqTickersAsync(q)
                    if not tickers:
                        return None
                    td = tickers[0]
                    bid = float(td.bid) if td.bid and td.bid == td.bid and td.bid > 0 else 0.0
                    ask = float(td.ask) if td.ask and td.ask == td.ask and td.ask > 0 else 0.0
                    mid = (bid + ask) / 2
                    spread_pct = ((ask - bid) / ask) if ask > 0 else 1.0
                    ref = _from_ib_contract(q)
                    ref.qualified = True
                    expiry_fmt = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
                    return OptionCandidate(
                        symbol=ticker, expiry=expiry_fmt, strike=strike, right=right,
                        bid=bid, ask=ask, mid=mid, spread_pct=spread_pct,
                        open_interest=None, volume=None,
                        multiplier=int(q.multiplier or 100), contract_ref=ref,
                    )
                except Exception:
                    return None

            results = await asyncio.gather(*[
                _qualify_and_quote(e, s, r) for e, s, r in pre_filtered
            ])
            candidates = [c for c in results if c is not None]

            if len(candidates) < 2:
                self._read_breaker._record_failure()
                raise IBGatewayUnavailable("chain_lookup_insufficient_candidates")

            self._read_breaker._record_success()
            return candidates
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_chain failed: {exc}") from exc

    async def _fetch_spot(self, ticker: str) -> float:
        return await self.get_quote(ticker)

    async def cancel_order(self, trade, timeout: float = 5.0) -> bool:
        import time as _time
        self._ib.cancelOrder(trade.order)
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            status = trade.orderStatus.status
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                return True
        logger.warning("cancel_order: timed out waiting for cancel confirmation")
        return False

    async def get_option_ask(self, contract_ref: BrokerContractRef) -> tuple[float, float]:
        self._read_breaker.check()
        try:
            ib_contract = _to_ib_contract(contract_ref)
            tickers = await self._ib.reqTickersAsync(ib_contract)
            if not tickers:
                return 0.0, float("inf")
            td = tickers[0]
            ask = float(td.ask) if td.ask and td.ask == td.ask and td.ask > 0 else 0.0
            self._read_breaker._record_success()
            return ask, 0.0
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_option_ask failed: {exc}") from exc

    async def get_account_summary(self) -> AccountSummary:
        self._read_breaker.check()
        try:
            # accountSummary() reads ib_insync's cached account values (updated on connect)
            summary = self._ib.accountSummary()
            if not summary:
                summary = await self._ib.reqAccountSummaryAsync()
            values = {item.tag: item.value for item in summary}
            result = AccountSummary(
                buying_power=float(values.get("BuyingPower", 0)),
                net_liquidation=float(values.get("NetLiquidation", 0)),
                currency=values.get("Currency", "USD"),
            )
            self._read_breaker._record_success()
            return result
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_account_summary failed: {exc}") from exc

    async def get_quote(self, ticker: str) -> float:
        self._read_breaker.check()
        try:
            from ib_insync import Stock
            stock = Stock(ticker, "SMART", "USD")
            qualified = await self._ib.qualifyContractsAsync(stock)
            if not qualified:
                raise IBGatewayUnavailable(f"could not qualify equity {ticker}")
            tickers = await self._ib.reqTickersAsync(qualified[0])
            if not tickers:
                raise IBGatewayUnavailable(f"no ticker data for {ticker}")
            td = tickers[0]
            # nan-safe price selection: ask → last → close
            for price in (td.ask, td.last, td.close):
                if price and price == price and price > 0:
                    self._read_breaker._record_success()
                    return float(price)
            raise IBGatewayUnavailable(f"no valid price for {ticker}")
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_quote failed: {exc}") from exc

    async def get_open_orders(self) -> list:
        self._read_breaker.check()
        try:
            if not self._ib:
                return []
            orders = self._ib.openOrders()
            self._read_breaker._record_success()
            return orders
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_open_orders failed: {exc}") from exc

    async def place_order(
        self,
        contract_ref: BrokerContractRef,
        order: PreparedOrder,
        client_order_id: str,
    ):
        self._assert_paper_guard()
        self._write_breaker.check()
        if not contract_ref.qualified:
            raise ValueError("contract_ref must be qualified before place_order")
        try:
            from ib_insync import LimitOrder
            ib_contract = _to_ib_contract(contract_ref)
            ib_order = LimitOrder(
                action=order.action,
                totalQuantity=order.quantity,
                lmtPrice=order.limit_price,
                tif=order.tif,
                orderRef=client_order_id,
            )
            trade = self._ib.placeOrder(ib_contract, ib_order)
            self._write_breaker._record_success()
            logger.info("Placed order %s: %s x%s @ %s",
                        client_order_id, contract_ref.symbol, order.quantity, order.limit_price)
            return trade
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._write_breaker._record_failure()
            raise IBGatewayUnavailable(f"place_order failed: {exc}") from exc

    async def wait_fill(self, trade, timeout: float) -> FillResult:
        deadline = time.monotonic() + timeout
        broker_order_id = str(trade.order.orderId)
        submitted_qty = trade.order.totalQuantity
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            # ib_insync updates trade status via the running asyncio event loop
            filled = trade.orderStatus.filled
            remaining = trade.orderStatus.remaining
            status_str = trade.orderStatus.status
            if status_str == "Filled":
                return FillResult(
                    status=FillStatus.FILLED,
                    broker_order_id=broker_order_id,
                    perm_id=trade.order.permId or None,
                    submitted_qty=int(submitted_qty),
                    filled_qty=int(filled),
                    remaining_qty=int(remaining),
                    avg_fill_price=trade.orderStatus.avgFillPrice or None,
                    last_status=status_str,
                    status_timestamp=datetime.now(timezone.utc).isoformat(),
                )
            if status_str in ("Cancelled", "ApiCancelled"):
                return FillResult(
                    status=FillStatus.CANCELLED,
                    broker_order_id=broker_order_id,
                    perm_id=trade.order.permId or None,
                    submitted_qty=int(submitted_qty),
                    filled_qty=int(filled),
                    remaining_qty=int(remaining),
                    avg_fill_price=trade.orderStatus.avgFillPrice or None,
                    last_status=status_str,
                    status_timestamp=datetime.now(timezone.utc).isoformat(),
                )
            if status_str == "Inactive":
                return FillResult(
                    status=FillStatus.REJECTED,
                    broker_order_id=broker_order_id,
                    perm_id=trade.order.permId or None,
                    submitted_qty=int(submitted_qty),
                    filled_qty=int(filled),
                    remaining_qty=int(remaining),
                    avg_fill_price=None,
                    last_status=status_str,
                    status_timestamp=datetime.now(timezone.utc).isoformat(),
                )
        # Timeout — does NOT trip circuit breaker
        self._record_fill_timeout()
        filled = trade.orderStatus.filled
        return FillResult(
            status=FillStatus.TIMED_OUT_PENDING,
            broker_order_id=broker_order_id,
            perm_id=trade.order.permId or None,
            submitted_qty=int(submitted_qty),
            filled_qty=int(filled),
            remaining_qty=int(submitted_qty - filled),
            avg_fill_price=trade.orderStatus.avgFillPrice or None,
            last_status=trade.orderStatus.status,
            status_timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _record_fill_timeout(self) -> None:
        pass  # fill timeouts never trip breakers — intentional no-op

    def _assert_paper_guard(self) -> None:
        p = self._policy.ib_gateway
        if p.mode != "paper":
            raise LiveTradingBlocked("mode is not 'paper'")
        if p.port not in (7497, 4002):
            raise LiveTradingBlocked(f"port {p.port} is not a paper trading port (7497/4002)")
        if self._account_id:
            prefix_ok = any(self._account_id.startswith(pfx) for pfx in p.paper_account_prefixes)
            if not prefix_ok:
                raise LiveTradingBlocked(
                    f"account {self._account_id} not in allowed paper prefixes {p.paper_account_prefixes}"
                )


def _to_ib_contract(ref: BrokerContractRef):
    from ib_insync import Contract
    c = Contract()
    c.symbol = ref.symbol
    c.secType = ref.sec_type
    c.exchange = ref.exchange
    c.currency = ref.currency
    if ref.con_id:
        c.conId = ref.con_id
    if ref.expiry:
        c.lastTradeDateOrContractMonth = ref.expiry
    if ref.strike is not None:
        c.strike = ref.strike
    if ref.right:
        c.right = ref.right
    if ref.multiplier:
        c.multiplier = ref.multiplier
    if ref.local_symbol:
        c.localSymbol = ref.local_symbol
    if ref.trading_class:
        c.tradingClass = ref.trading_class
    return c


def _from_ib_contract(c) -> BrokerContractRef:
    return BrokerContractRef(
        symbol=c.symbol,
        sec_type=c.secType,
        exchange=c.exchange or "SMART",
        currency=c.currency or "USD",
        con_id=c.conId or None,
        expiry=c.lastTradeDateOrContractMonth or None,
        strike=float(c.strike) if c.strike else None,
        right=c.right or None,
        multiplier=str(c.multiplier) if c.multiplier else None,
        local_symbol=c.localSymbol or None,
        trading_class=c.tradingClass or None,
        qualified=False,
    )
