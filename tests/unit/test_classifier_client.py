import pytest
from infra.llm.classifier_client import AnthropicClassifierClient


class FakeContent:
    def __init__(self, text: str):
        self.text = text


class FakeResponse:
    def __init__(self, text: str):
        self.content = [FakeContent(text)]


class FakeAnthropic:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict] = []
        self.messages = self  # mimic SDK's nested attribute

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._text)


@pytest.mark.asyncio
async def test_classifier_parses_clean_json():
    fake = FakeAnthropic('{"is_entry": true, "ticker": "X", "side": "long", '
                        '"bucket": "LOW", "confidence": 0.85, "reason": "explicit"}')
    client = AnthropicClassifierClient(fake)
    out = await client.classify(
        system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert out["bucket"] == "LOW"
    assert out["confidence"] == 0.85


@pytest.mark.asyncio
async def test_classifier_extracts_json_from_wrapped_text():
    fake = FakeAnthropic('Here is the result:\n{"is_entry": false, "ticker": null, '
                        '"side": "none", "bucket": "SKIP", "confidence": 0.9, "reason": "x"}')
    client = AnthropicClassifierClient(fake)
    out = await client.classify(system=[], model="m", messages=[])
    assert out["bucket"] == "SKIP"


@pytest.mark.asyncio
async def test_classifier_raises_on_unparseable_response():
    fake = FakeAnthropic("totally not json here")
    client = AnthropicClassifierClient(fake)
    with pytest.raises(ValueError, match="parse"):
        await client.classify(system=[], model="m", messages=[])


@pytest.mark.asyncio
async def test_classifier_passes_timeout_to_create():
    fake = FakeAnthropic('{"is_entry": true, "ticker": "X", "side": "long", '
                        '"bucket": "LOW", "confidence": 0.9, "reason": "x"}')
    client = AnthropicClassifierClient(fake, timeout_seconds=8.0)
    await client.classify(system=[], model="m", messages=[])
    assert fake.calls[0]["timeout"] == 8.0
