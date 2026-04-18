import pytest
import aiosqlite
from infra.storage.db import SCHEMA
from infra.storage.idempotency_store import IdempotencyStore
from infra.storage.trace_store import TraceStore


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[str] = []
    async def send_message(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        yield conn


@pytest.fixture
def telegram():
    return FakeTelegramClient()
