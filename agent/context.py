from dataclasses import dataclass, field
from typing import Any


@dataclass
class Context:
    trace_id: str
    event_id: str
    data: dict[str, Any] = field(default_factory=dict)

    def update(self, updates: dict[str, Any]) -> None:
        self.data.update(updates)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass
class SkillResult:
    status: str  # "success" | "skip" | "fail"
    updates: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("success", "skip", "fail"):
            raise ValueError(f"Invalid status: {self.status}")
