from __future__ import annotations
import math


def marketable_limit(ask: float, cap_pct: float) -> float:
    """Round-up-to-the-penny marketable BUY limit price.

    Returns ``ask * (1 + cap_pct)`` rounded UP to the nearest cent so the limit is
    always >= the live ask (stays marketable — fills at the NBBO immediately) while
    capping how far the order will chase a moving ask.
    """
    return math.ceil(ask * (1.0 + cap_pct) * 100) / 100


def marketable_sell_limit(price: float, cap_pct: float) -> float:
    """Round-down-to-the-penny marketable SELL limit price.

    Returns ``price * (1 - cap_pct)`` rounded DOWN to the nearest cent so the limit
    is always <= the reference price (stays marketable — sells into the bid
    immediately) while flooring how far below market the order will sell.
    """
    return math.floor(price * (1.0 - cap_pct) * 100) / 100
