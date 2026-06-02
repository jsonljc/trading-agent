import pytest

from agent.replay.recorded_llm import RecordedClassifierClient


def _messages(text):
    return [{"role": "user", "content": text}]


@pytest.mark.asyncio
async def test_hit_returns_recorded_response():
    recorded = {
        "buy AAPL here, core position": {
            "is_entry": True, "ticker": "AAPL", "side": "long",
            "bucket": "HIGH", "confidence": 0.95, "reason": "core",
        }
    }
    client = RecordedClassifierClient(recorded)
    out = await client.classify(
        system=[], model="m",
        messages=_messages("buy AAPL here, core position"),
    )
    assert out["bucket"] == "HIGH"
    assert out["ticker"] == "AAPL"
    assert client.misses == 0
    assert client.hits == 1


@pytest.mark.asyncio
async def test_miss_returns_skip_default_and_counts():
    client = RecordedClassifierClient({})
    out = await client.classify(
        system=[], model="m", messages=_messages("unseen message"),
    )
    assert out == {"is_entry": False, "bucket": "SKIP", "confidence": 0.0}
    assert client.misses == 1
    assert client.hits == 0


@pytest.mark.asyncio
async def test_was_recorded_lookup():
    client = RecordedClassifierClient({"hello": {"bucket": "SKIP"}})
    assert client.was_recorded("hello") is True
    assert client.was_recorded("nope") is False
