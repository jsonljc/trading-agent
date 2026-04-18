import pytest
import aiosqlite
from pathlib import Path
from agent.context import Context
from infra.storage.db import SCHEMA
from infra.storage.idempotency_store import IdempotencyStore
from skills.risk.idempotency_check import IdempotencyCheck
from agent.policy import PolicyModel
import yaml


def make_policy():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    return PolicyModel.model_validate(yaml.safe_load(config_path.read_text()))


@pytest.fixture
async def store():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        yield IdempotencyStore(conn)


def make_ctx(fingerprint: str = "fp1") -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"message_fingerprint": fingerprint, "channel": "mystic"})
    return ctx


async def test_first_run_succeeds(store):
    skill = IdempotencyCheck(make_policy(), store)
    result = await skill.run(make_ctx("fp1"))
    assert result.status == "success"


async def test_second_run_skips(store):
    skill = IdempotencyCheck(make_policy(), store)
    await skill.run(make_ctx("fp1"))
    result = await skill.run(make_ctx("fp1"))
    assert result.status == "skip"
    assert "duplicate" in result.reason.lower()


async def test_different_fingerprints_both_succeed(store):
    skill = IdempotencyCheck(make_policy(), store)
    r1 = await skill.run(make_ctx("fp1"))
    r2 = await skill.run(make_ctx("fp2"))
    assert r1.status == "success"
    assert r2.status == "success"


async def test_missing_fingerprint_fails(store):
    skill = IdempotencyCheck(make_policy(), store)
    ctx = Context(trace_id="t1", event_id="e1")
    # No message_fingerprint set
    result = await skill.run(ctx)
    assert result.status == "fail"
    assert "fingerprint" in result.reason.lower()
