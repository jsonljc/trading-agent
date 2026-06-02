import pytest
from agent.context import Context
from skills.signal.message_normalizer import MessageNormalizer
from agent.policy import PolicyModel
import yaml


from pathlib import Path

def make_policy():
    config_path = Path(__file__).parents[2] / "config" / "policy.yaml"
    raw = yaml.safe_load(config_path.read_text())
    return PolicyModel.model_validate(raw)


def make_ctx(preview: str, channel: str = "mystic", author: str = "Mystic") -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "trigger_preview": preview,
        "channel": channel,
        "author": author,
        "received_at": "2026-04-18T10:00:00Z",
    })
    return ctx


async def test_normalizer_sets_required_fields():
    skill = MessageNormalizer(make_policy())
    ctx = make_ctx("Long $AVEX today's IPO")
    result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates["trigger_preview"] == "Long $AVEX today's IPO"
    assert result.updates["full_message_text"] == "Long $AVEX today's IPO"
    assert result.updates["capture_mode"] == "preview"
    assert len(result.updates["message_fingerprint"]) == 16


async def test_normalizer_strips_excess_whitespace():
    skill = MessageNormalizer(make_policy())
    ctx = make_ctx("  Long   $AVEX   today  ")
    result = await skill.run(ctx)
    assert result.updates["full_message_text"] == "Long $AVEX today"


async def test_normalizer_fingerprint_is_deterministic():
    skill = MessageNormalizer(make_policy())
    ctx1 = make_ctx("Long $AVEX", channel="mystic", author="Mystic")
    ctx2 = make_ctx("Long $AVEX", channel="mystic", author="Mystic")
    r1 = await skill.run(ctx1)
    r2 = await skill.run(ctx2)
    assert r1.updates["message_fingerprint"] == r2.updates["message_fingerprint"]


async def test_normalizer_different_authors_produce_different_fingerprints():
    skill = MessageNormalizer(make_policy())
    ctx1 = make_ctx("Long $AVEX", author="Alice")
    ctx2 = make_ctx("Long $AVEX", author="Bob")
    r1 = await skill.run(ctx1)
    r2 = await skill.run(ctx2)
    assert r1.updates["message_fingerprint"] != r2.updates["message_fingerprint"]


async def test_normalizer_sets_intent_timestamp():
    skill = MessageNormalizer(make_policy())
    ctx = make_ctx("Long $AVEX")
    result = await skill.run(ctx)
    assert result.updates["intent_timestamp"] == "2026-04-18T10:00:00Z"


async def test_compute_fingerprint_helper_matches_skill_and_normalizes_whitespace():
    from skills.signal.message_normalizer import compute_fingerprint
    fp_a = compute_fingerprint("mystic", "Mystic", "long  $SPY   now")
    fp_b = compute_fingerprint("mystic", "Mystic", "long $SPY now")
    assert fp_a == fp_b
    assert len(fp_a) == 16
    # The helper must produce the SAME value the skill writes, so the
    # signal_events row matches the idempotency fingerprint.
    skill = MessageNormalizer(make_policy())
    ctx = make_ctx("long  $SPY   now", channel="mystic", author="Mystic")
    result = await skill.run(ctx)
    assert result.updates["message_fingerprint"] == fp_a
