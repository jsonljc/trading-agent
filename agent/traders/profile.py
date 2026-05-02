from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import yaml


VALID_BUCKETS = {"LOW", "HIGH", "SKIP"}


@dataclass(frozen=True)
class ConvictionExample:
    msg: str
    bucket: str
    why: str


@dataclass(frozen=True)
class TraderProfile:
    handle: str
    display_name: str
    discord_author_pattern: str
    alert_mention: str
    require_alert_mention: bool
    bot_authors_to_skip: list[str]
    auto_execute: bool
    size_in_message: bool
    prefer_message_size: bool
    classifier_model: str
    availability_phrases: list[str]
    conviction_examples: list[ConvictionExample]


def load_profile(path: Path) -> TraderProfile:
    raw = yaml.safe_load(path.read_text())
    examples = []
    for e in raw.get("conviction_examples", []):
        if e["bucket"] not in VALID_BUCKETS:
            raise ValueError(f"invalid bucket {e['bucket']!r} in {path}")
        examples.append(ConvictionExample(msg=e["msg"], bucket=e["bucket"], why=e.get("why", "")))
    return TraderProfile(
        handle=raw["handle"],
        display_name=raw["display_name"],
        discord_author_pattern=raw["discord_author_pattern"],
        alert_mention=raw["alert_mention"],
        require_alert_mention=bool(raw.get("require_alert_mention", True)),
        bot_authors_to_skip=list(raw.get("bot_authors_to_skip", [])),
        auto_execute=bool(raw.get("auto_execute", False)),
        size_in_message=bool(raw.get("size_in_message", False)),
        prefer_message_size=bool(raw.get("prefer_message_size", True)),
        classifier_model=raw.get("classifier_model", "claude-haiku-4-5"),
        availability_phrases=list(raw.get("availability_phrases", [])),
        conviction_examples=examples,
    )


def load_all_profiles(directory: Path) -> list[TraderProfile]:
    return [load_profile(p) for p in sorted(directory.glob("*.yaml"))]
