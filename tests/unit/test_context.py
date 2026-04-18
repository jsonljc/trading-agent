import pytest
from agent.context import Context, SkillResult


def test_context_update_merges_data():
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"ticker": "AVEX"})
    ctx.update({"conviction": "high"})
    assert ctx.data["ticker"] == "AVEX"
    assert ctx.data["conviction"] == "high"


def test_context_get_returns_default():
    ctx = Context(trace_id="t1", event_id="e1")
    assert ctx.get("missing", "default") == "default"


def test_skill_result_rejects_invalid_status():
    with pytest.raises(ValueError):
        SkillResult(status="invalid")


def test_skill_result_success():
    r = SkillResult(status="success", updates={"x": 1})
    assert r.status == "success"
    assert r.updates["x"] == 1


def test_skill_result_skip_with_reason():
    r = SkillResult(status="skip", reason="no action detected")
    assert r.status == "skip"
    assert r.reason == "no action detected"
