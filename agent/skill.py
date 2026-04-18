from abc import ABC, abstractmethod
from agent.context import Context, SkillResult


class Skill(ABC):
    name: str

    @abstractmethod
    async def run(self, ctx: Context) -> SkillResult: ...
