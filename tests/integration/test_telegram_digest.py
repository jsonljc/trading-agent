import pytest
from agent.context import Context
from skills.posttrade.telegram_digest import TelegramDigest


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[str] = []
    async def send_message(self, text: str) -> None:
        self.sent.append(text)


def make_ctx(**kwargs) -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "channel": "mystic", "author": "Mystic",
        "full_message_text": "Long $AVEX today",
        "confidence": 0.85,
        "ticker": "AVEX", "bucket": "HIGH",
        "size_pct": 0.10,
        **kwargs,
    })
    return ctx


async def test_digest_sends_signal_summary():
    client = FakeTelegramClient()
    skill = TelegramDigest(client, mode="signal_only")
    result = await skill.run(make_ctx())
    assert result.status == "success"
    assert len(client.sent) == 1
    msg = client.sent[0]
    assert "AVEX" in msg
    assert "Confidence:" in msg
    assert "0.85" in msg
    assert "HIGH" in msg
    # Sizing pct is no longer shown pre-trade — it's resolved in phase2b
    # (per-channel buckets) after the signal digest fires.


async def test_digest_includes_trace_id():
    client = FakeTelegramClient()
    skill = TelegramDigest(client, mode="signal_only")
    ctx = make_ctx()
    ctx.trace_id = "trace-abc"
    await skill.run(ctx)
    assert "trace-abc" in client.sent[0]


async def test_error_digest():
    client = FakeTelegramClient()
    skill = TelegramDigest(client, mode="signal_only")
    ctx = make_ctx()
    await skill.send_error_digest(ctx, "ticker ambiguous")
    assert "ERROR" in client.sent[0]
    assert "ticker ambiguous" in client.sent[0]


def test_is_broker_unavailable_skip_recognises_circuit_open():
    ctx = make_ctx(bucket="HIGH")
    assert TelegramDigest.is_broker_unavailable_skip(
        ctx, "ambiguous_signal: ticker 'ADEA' could not be validated: circuit open"
    )


def test_is_broker_unavailable_skip_ignores_non_actionable_buckets():
    ctx = make_ctx(bucket="SKIP")
    assert not TelegramDigest.is_broker_unavailable_skip(
        ctx, "ambiguous_signal: circuit open"
    )


def test_is_broker_unavailable_skip_ignores_non_broker_reasons():
    ctx = make_ctx(bucket="HIGH")
    assert not TelegramDigest.is_broker_unavailable_skip(
        ctx, "no_entry:bucket=SKIP"
    )


async def test_missed_signal_alert_includes_trader_ticker_bucket():
    client = FakeTelegramClient()
    skill = TelegramDigest(client, mode="signal_only")
    ctx = make_ctx(
        bucket="HIGH", ticker="ADEA", side="long", trader_handle="mystic"
    )
    await skill.send_missed_signal_alert(
        ctx, "ambiguous_signal: ticker 'ADEA' could not be validated: circuit open"
    )
    assert len(client.sent) == 1
    msg = client.sent[0]
    assert "MISSED SIGNAL" in msg
    assert "mystic" in msg
    assert "ADEA" in msg
    assert "HIGH" in msg
    assert "circuit open" in msg




async def test_order_rejected_alert_is_distinct():
    client = FakeTelegramClient()
    skill = TelegramDigest(client, mode="signal_only")
    assert TelegramDigest.is_order_rejected("shares_rejected:Inactive")
    assert TelegramDigest.is_order_rejected("options_rejected:Inactive")
    assert TelegramDigest.is_order_rejected("broker_rejected:Inactive")
    assert not TelegramDigest.is_order_rejected("shares_not_filled:Submitted")
    assert not TelegramDigest.is_order_rejected("options_not_filled:Submitted")
    await skill.send_order_rejected_alert(make_ctx(side="long"),
                                          "shares_rejected:Inactive")
    assert len(client.sent) == 1
    assert "ORDER REJECTED" in client.sent[0]
    assert "DLQ" in client.sent[0]
