from __future__ import annotations
from dataclasses import dataclass
import re


_ENTRY_VERBS = re.compile(
    r"\b(open|opening|opened|added|adding|bought|initiating|initiated|"
    r"joining|joined|loading|took|grabbed|picked up|started|scaled in)\b",
    re.IGNORECASE,
)
_SIZE_PCT = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:pos|position|weighting|wt)?",
    re.IGNORECASE,
)
_TICKERS = re.compile(r"(?<!\w)\$?([A-Z]{1,6})\b")
_DOLLAR_TICKERS = re.compile(r"\$([A-Z]{1,6})\b")


@dataclass(frozen=True)
class Features:
    stated_size_pct: float | None
    entry_verb_present: bool
    tickers_in_msg: tuple[str, ...]
    embed_present: bool
    msg_length: int
    is_thread_reply: bool
    availability_phrase: str | None = None


def extract_features(
    msg: str,
    *,
    is_thread_reply: bool = False,
    embed_present: bool = False,
    availability_phrases: list[str] | tuple[str, ...] | None = None,
) -> Features:
    size_match = _SIZE_PCT.search(msg)
    stated_size = float(size_match.group(1)) if size_match else None

    entry_verb = bool(_ENTRY_VERBS.search(msg))

    dollar_tickers = _DOLLAR_TICKERS.findall(msg)
    if dollar_tickers:
        tickers = tuple(dict.fromkeys(dollar_tickers))
    else:
        tickers = tuple(
            dict.fromkeys(
                t for t in _TICKERS.findall(msg)
                if t.isupper() and 2 <= len(t) <= 6
            )
        )

    availability = None
    for phrase in availability_phrases or ():
        if phrase.lower() in msg.lower():
            availability = phrase
            break

    return Features(
        stated_size_pct=stated_size,
        entry_verb_present=entry_verb,
        tickers_in_msg=tickers,
        embed_present=embed_present,
        msg_length=len(msg),
        is_thread_reply=is_thread_reply,
        availability_phrase=availability,
    )
