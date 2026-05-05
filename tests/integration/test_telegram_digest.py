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


