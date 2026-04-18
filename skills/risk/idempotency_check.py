from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel
from infra.storage.idempotency_store import IdempotencyStore


class IdempotencyCheck(Skill):
    name = "idempotency_check"

    def __init__(self, policy: PolicyModel, store: IdempotencyStore) -> None:
        self._policy = policy
        self._store = store

    async def run(self, ctx: Context) -> SkillResult:
        fingerprint = ctx.get("message_fingerprint", "")
        key = f"{fingerprint}:signal_only"

        inserted = await self._store.insert_if_new(key, ctx.event_id, "unknown", "signal_only")
        if not inserted:
            return SkillResult(status="skip", reason=f"duplicate signal (key={key})")

        return SkillResult(status="success", updates={"idempotency_key": key})
