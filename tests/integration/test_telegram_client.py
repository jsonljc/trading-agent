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


async def test_real_client_sends_correct_payload(mocker):
    mock_response = mocker.MagicMock()
    mock_response.is_error = False
    mock_response.status_code = 200

    mock_post = mocker.AsyncMock(return_value=mock_response)
    mock_http_client = mocker.MagicMock()
    mock_http_client.post = mock_post
    mock_http_client.__aenter__ = mocker.AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = mocker.AsyncMock(return_value=False)

    mocker.patch("infra.telegram.client.httpx.AsyncClient", return_value=mock_http_client)

    client = TelegramClient("mytoken", "chat123")
    await client.send_message("hello world")

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "sendMessage" in call_kwargs[0][0]
    payload = call_kwargs[1]["json"]
    assert payload["chat_id"] == "chat123"
    assert payload["text"] == "hello world"
    assert payload["parse_mode"] == "HTML"
