from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class FillStatus(str, Enum):
    FILLED = "filled"
    PARTIAL_FILL = "partial_fill"
    SUBMITTED_UNFILLED = "submitted_unfilled"
    TIMED_OUT_PENDING = "timed_out_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ExecutionMode(str, Enum):
    EXECUTE_NOW = "execute_now"
    QUEUE_FOR_SESSION = "queue_for_session"
    REJECT = "reject"


@dataclass
class BrokerContractRef:
    symbol: str
    sec_type: str               # 'OPT' | 'STK'
    exchange: str
    currency: str
    con_id: int | None = None
    expiry: str | None = None   # YYYYMMDD
    strike: float | None = None
    right: str | None = None    # 'C' | 'P'
    multiplier: str | None = None
    local_symbol: str | None = None
    trading_class: str | None = None
    qualified: bool = False


@dataclass
class OptionCandidate:
    symbol: str
    expiry: str             # YYYY-MM-DD
    strike: float
    right: str              # 'C' | 'P'
    bid: float
    ask: float
    mid: float
    spread_pct: float
    open_interest: int | None
    volume: int | None
    multiplier: int
    contract_ref: BrokerContractRef


@dataclass
class AccountSummary:
    buying_power: float
    net_liquidation: float
    currency: str


@dataclass
class PreparedOrder:
    action: str         # 'BUY'
    quantity: int
    order_type: str     # 'LMT' | 'MKT'
    limit_price: float | None
    tif: str            # 'DAY'


@dataclass
class FillResult:
    status: FillStatus
    broker_order_id: str
    perm_id: int | None
    submitted_qty: int
    filled_qty: int
    remaining_qty: int
    avg_fill_price: float | None
    last_status: str
    status_timestamp: str
