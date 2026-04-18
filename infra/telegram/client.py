import httpx


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
            )
            resp.raise_for_status()
