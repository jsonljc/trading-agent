from __future__ import annotations


def make_filled_intent(intent_id: str, *, channel: str, ticker: str,
                       fill_qty: int, seq: int = 0, fill_price: float = 100.0) -> dict:
    """A filled equity trade_intent row for TradeIntentStore.insert.

    `seq` makes filled_at lexically increasing so get_open_shares_positions
    (ORDER BY filled_at ASC, created_at ASC) is deterministic oldest-first.
    """
    base = "2026-06-26T14:30:00+00:00"
    filled_at = f"2026-06-26T14:30:00.{seq:06d}+00:00"
    return {
        "intent_id": intent_id, "event_id": intent_id.split(":")[0],
        "channel": channel, "ticker": ticker, "side": "long",
        "instrument_type": "equity", "conviction": "HIGH", "policy_state": "approved",
        "execution_state": "filled", "fill_qty": fill_qty, "fill_price": fill_price,
        "filled_at": filled_at, "signal_received_at": base, "intent_created_at": base,
        "created_at": base, "updated_at": base,
    }
