from __future__ import annotations
import math


def marketable_limit(ask: float, cap_pct: float) -> float:
    """Round-up-to-the-penny marketable BUY limit price.

    Returns ``ask * (1 + cap_pct)`` rounded UP to the nearest cent so the limit is
    always >= the live ask (stays marketable — fills at the NBBO immediately) while
    capping how far the order will chase a moving ask.
    """
    return math.ceil(ask * (1.0 + cap_pct) * 100) / 100
