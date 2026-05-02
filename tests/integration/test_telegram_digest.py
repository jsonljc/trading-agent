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
        "intent": "LONG_SIGNAL", "confidence": "high",
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
    assert "LONG_SIGNAL" in msg
    assert "high" in msg.lower()
    assert "10%" in msg or "0.10" in msg


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


@pytest.mark.asyncio
async def test_bootstrap_review_digest_includes_classification_details(telegram):
    digest = TelegramDigest(telegram, mode="signal_only")
    ctx = Context(trace_id="t1", event_id="e1", data={
        "trader_handle": "mystic",
        "author": "UndefinedMystic",
        "channel": "alerts",
        "ticker": "INDI",
        "bucket": "LOW",
        "confidence": 0.72,
        "size_pct": 0.05,
        "classifier_reason": "small + swing trade self-label",
        "full_message_text": "i opened a small swing trade in INDI",
    })
    await digest.send_bootstrap_review_digest(ctx)
    assert len(telegram.sent) == 1
    body = telegram.sent[0]
    assert "BOOTSTRAP REVIEW" in body
    assert "mystic" in body
    assert "INDI" in body
    assert "LOW" in body
    assert "5%" in body
    assert "0.72" in body
