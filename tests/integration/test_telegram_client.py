import pytest
from infra.telegram.client import TelegramClient


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent.append(text)


async def test_fake_client_captures_messages():
    client = FakeTelegramClient()
    await client.send_message("hello")
    await client.send_message("world")
    assert client.sent == ["hello", "world"]
