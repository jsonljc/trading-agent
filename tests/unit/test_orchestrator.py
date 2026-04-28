import pytest
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.orchestrator import Orchestrator


class FakeTraceStore:
    def __init__(self):
        self.started = []
        self.finished = []
        self.skills = []

    async def start(self, trace_id, event_id):
        self.started.append((trace_id, event_id))

    async def finish(self, trace_id, status, reason=None):
        self.finished.append((trace_id, status, reason))

    async def record_skill(self, trace_id, skill_name, status, output):
        self.skills.append((skill_name, status))


class SuccessSkill(Skill):
    name = "success_skill"
    async def run(self, ctx):
        return SkillResult(status="success", updates={"ran": True})


class SkipSkill(Skill):
    name = "skip_skill"
    async def run(self, ctx):
        return SkillResult(status="skip", reason="no action")


class FailSkill(Skill):
    name = "fail_skill"
    async def run(self, ctx):
        return SkillResult(status="fail", reason="broken")


class ShouldNotRunSkill(Skill):
    name = "should_not_run"
    async def run(self, ctx):
        raise AssertionError("This skill should not have run")


async def test_success_chain_runs_all_skills():
    store = FakeTraceStore()
    orch = Orchestrator([SuccessSkill(), SuccessSkill()], store)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert ctx.data["ran"] is True
    assert store.finished[0][1] == "success"


async def test_skip_stops_pipeline():
    store = FakeTraceStore()
    orch = Orchestrator([SkipSkill(), ShouldNotRunSkill()], store)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert store.finished[0][1] == "skipped"
    assert len([s for s in store.skills if s[0] == "should_not_run"]) == 0


async def test_fail_stops_pipeline():
    store = FakeTraceStore()
    orch = Orchestrator([FailSkill(), ShouldNotRunSkill()], store)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert store.finished[0][1] == "failed"
    assert store.finished[0][2] == "broken"


async def test_on_fail_callback_fires():
    store = FakeTraceStore()
    received = []
    async def on_fail(ctx, reason):
        received.append(reason)
    orch = Orchestrator([FailSkill()], store, on_fail=on_fail)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert received == ["broken"]


async def test_unhandled_exception_marks_failed():
    class CrashSkill(Skill):
        name = "crash"
        async def run(self, ctx):
            raise RuntimeError("oops")
    store = FakeTraceStore()
    orch = Orchestrator([CrashSkill()], store)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert store.finished[0][1] == "failed"
    assert "oops" in store.finished[0][2]


async def test_on_skip_callback_fires():
    store = FakeTraceStore()
    received = []
    async def on_skip(ctx, reason):
        received.append(reason)
    orch = Orchestrator([SkipSkill()], store, on_skip=on_skip)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert received == ["no action"]


async def test_unhandled_exception_fires_on_fail():
    class CrashSkill(Skill):
        name = "crash"
        async def run(self, ctx):
            raise RuntimeError("boom")
    store = FakeTraceStore()
    received = []
    async def on_fail(ctx, reason):
        received.append(reason)
    orch = Orchestrator([CrashSkill()], store, on_fail=on_fail)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert len(received) == 1
    assert "boom" in received[0]


async def test_on_success_exception_does_not_trigger_on_fail():
    """A pipeline that succeeded must not be reported as failed because
    on_success itself raised. Otherwise audit_writer.write hiccups would
    mislabel real fills as failures and page the operator."""
    class OkSkill(Skill):
        name = "ok"
        async def run(self, ctx):
            return SkillResult(status="success")

    store = FakeTraceStore()
    fails: list = []
    async def on_success(ctx):
        raise RuntimeError("audit DB locked")
    async def on_fail(ctx, reason):
        fails.append(reason)

    orch = Orchestrator([OkSkill()], store, on_fail=on_fail, on_success=on_success)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)

    assert fails == [], f"on_fail should NOT fire when only on_success raised, got {fails}"
    assert store.finished == [("t1", "success", None)], (
        f"trace must remain marked success, got {store.finished}"
    )


async def test_ctx_updates_accumulate_across_skills():
    class SkillA(Skill):
        name = "skill_a"
        async def run(self, ctx):
            return SkillResult(status="success", updates={"a": 1})

    class SkillB(Skill):
        name = "skill_b"
        async def run(self, ctx):
            return SkillResult(status="success", updates={"b": 2})

    store = FakeTraceStore()
    orch = Orchestrator([SkillA(), SkillB()], store)
    ctx = Context(trace_id="t1", event_id="e1")
    await orch.run(ctx)
    assert ctx.data["a"] == 1
    assert ctx.data["b"] == 2
