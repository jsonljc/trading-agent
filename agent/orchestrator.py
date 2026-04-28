import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, skills: list[Skill], trace_store, on_skip=None, on_fail=None, on_success=None) -> None:
        self._skills = skills
        self._trace_store = trace_store
        self._on_skip = on_skip       # async callable(ctx, reason)
        self._on_fail = on_fail       # async callable(ctx, reason)
        self._on_success = on_success # async callable(ctx)

    async def run(self, ctx: Context) -> Context:
        try:
            await self._trace_store.start(ctx.trace_id, ctx.event_id)
            for skill in self._skills:
                result: SkillResult = await skill.run(ctx)
                await self._trace_store.record_skill(
                    ctx.trace_id, skill.name, result.status, result.updates
                )
                if result.updates:
                    ctx.update(result.updates)

                if result.status == "skip":
                    logger.info("Pipeline skipped at %s: %s", skill.name, result.reason)
                    await self._trace_store.finish(ctx.trace_id, "skipped")
                    if self._on_skip:
                        await self._on_skip(ctx, result.reason)
                    return ctx

                if result.status == "fail":
                    logger.error("Pipeline failed at %s: %s", skill.name, result.reason)
                    await self._trace_store.finish(ctx.trace_id, "failed", result.reason)
                    if self._on_fail:
                        await self._on_fail(ctx, result.reason)
                    return ctx

            await self._trace_store.finish(ctx.trace_id, "success")
        except Exception as exc:
            reason = f"unhandled exception in pipeline: {exc}"
            logger.exception(reason)
            await self._trace_store.finish(ctx.trace_id, "failed", reason)
            if self._on_fail:
                try:
                    await self._on_fail(ctx, reason)
                except Exception:
                    logger.exception("on_fail callback itself failed")
            return ctx
        # on_success runs OUTSIDE the try so a hiccup here (e.g. audit DB
        # locked) does not retroactively mislabel a successful run as failed.
        if self._on_success:
            try:
                await self._on_success(ctx)
            except Exception:
                logger.exception("on_success callback failed (pipeline already succeeded)")
        return ctx
