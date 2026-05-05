from __future__ import annotations
from pathlib import Path
from agent.traders.profile import TraderProfile, load_all_profiles


class TraderRegistry:
    def __init__(self, profiles: list[TraderProfile]) -> None:
        self._by_author: dict[str, TraderProfile] = {}
        self._bot_authors: dict[str, TraderProfile] = {}
        for p in profiles:
            self._by_author[p.discord_author_pattern] = p
            for bot in p.bot_authors_to_skip:
                self._bot_authors[bot] = p

    def lookup(self, author: str) -> TraderProfile | None:
        return self._by_author.get(author)

    def is_bot_author(self, author: str) -> TraderProfile | None:
        return self._bot_authors.get(author)

    def all(self) -> list[TraderProfile]:
        return list(self._by_author.values())

    @classmethod
    def from_dir(cls, directory: str | Path) -> "TraderRegistry":
        return cls(load_all_profiles(Path(directory)))
