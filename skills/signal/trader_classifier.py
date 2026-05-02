from __future__ import annotations
import dataclasses
import json
import logging
from typing import Protocol

from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.traders.registry import TraderRegistry
from skills.signal.feature_extractor import extract_features


logger = logging.getLogger(__name__)

HIGH_CONF_THRESHOLD = 0.80
DROP_CONF_THRESHOLD = 0.50
SIZE_LOW = 0.05
SIZE_HIGH = 0.10
MAX_STATED_SIZE = 0.10  # cap at 10%
SIZE_HIGH_SHORTCUT_THRESHOLD = 0.075  # bucket=HIGH in shortcut path when size_pct >= this

_SYSTEM_PREAMBLE = """You classify Discord trading messages from a single trader into one of three buckets: HIGH, LOW, SKIP.

Definitions:
- HIGH: clearly high-conviction entry (deep thesis, "core", "upsize", "high conviction", multi-paragraph reasoning).
- LOW: any actionable entry that is not HIGH — starters, swing trades, "small", "stab", explicit small percentages, standard adds.
- SKIP: commentary, news headlines, watchlist, exits, portfolio recaps, "no position", "fyi", macro takes, sympathy plays without entry.

Return JSON only:
{"is_entry": bool, "ticker": "SYMBOL"|null, "side": "long"|"short"|"none", "bucket": "HIGH"|"LOW"|"SKIP", "confidence": 0.0-1.0, "reason": "one sentence"}

Examples for THIS specific trader:
"""


def _build_system_prompt(profile) -> str:
    examples_block = "\n".join(
        f"- MSG: {e.msg!r}\n  BUCKET: {e.bucket}\n  WHY: {e.why}"
        for e in profile.conviction_examples
    )
    return _SYSTEM_PREAMBLE + examples_block


class LLMClassifierClient(Protocol):
    async def classify(self, *, system: list, model: str, messages: list) -> dict: ...


class TraderClassifier(Skill):
    name = "TraderClassifier"

    def __init__(self, registry: TraderRegistry, llm: LLMClassifierClient) -> None:
        self._registry = registry
        self._llm = llm

    async def run(self, ctx: Context) -> SkillResult:
        handle = ctx.get("trader_handle")
        profile = next((p for p in self._registry.all() if p.handle == handle), None)
        if profile is None:
            return SkillResult(status="fail", reason=f"trader_profile_not_found:{handle}")

        msg = ctx.get("full_message_text", "")
        features = extract_features(
            msg,
            availability_phrases=profile.availability_phrases,
        )

        # Deterministic shortcut: stated size + entry verb + exactly one ticker.
        if (
            profile.prefer_message_size
            and features.stated_size_pct is not None
            and features.entry_verb_present
            and len(features.tickers_in_msg) == 1
        ):
            size_pct = min(features.stated_size_pct / 100.0, MAX_STATED_SIZE)
            # >=7.5% → HIGH bookkeeping; this is informational only since size_pct already won.
            bucket = "HIGH" if size_pct >= SIZE_HIGH_SHORTCUT_THRESHOLD else "LOW"
            updates = {
                "ticker": features.tickers_in_msg[0],
                "side": "long",
                "bucket": bucket,
                "confidence": 1.0,
                "size_pct": size_pct,
                "size_source": "shortcut_stated",
                "classifier_features_json": json.dumps(dataclasses.asdict(features)),
                "classifier_llm_response_json": None,
                "classifier_reason": "stated_size_in_message",
            }
            ctx.update(updates)
            return SkillResult(status="success", updates=updates)

        # LLM path.
        system_prompt = _build_system_prompt(profile)
        try:
            response = await self._llm.classify(
                system=[{"type": "text", "text": system_prompt,
                         "cache_control": {"type": "ephemeral"}}],
                model=profile.classifier_model,
                messages=[{"role": "user", "content": msg}],
            )
        except Exception as exc:
            logger.exception("trader_classifier llm error: %s", exc)
            return SkillResult(status="fail", reason=f"llm_error:{exc}")

        bucket = response.get("bucket", "SKIP")
        confidence = float(response.get("confidence", 0.0))
        ticker = response.get("ticker")
        side = response.get("side", "none")
        reason = response.get("reason", "")

        features_json = json.dumps(dataclasses.asdict(features))
        llm_json = json.dumps(response)

        if bucket == "SKIP" or not response.get("is_entry"):
            updates = {
                "bucket": "SKIP", "confidence": confidence,
                "size_pct": 0.0, "size_source": "skip",
                "classifier_features_json": features_json,
                "classifier_llm_response_json": llm_json,
                "classifier_reason": reason,
            }
            ctx.update(updates)
            return SkillResult(status="success", updates=updates,
                               reason=f"classifier_skip:{reason}")

        if confidence < DROP_CONF_THRESHOLD:
            updates = {
                "bucket": bucket, "confidence": confidence,
                "size_pct": 0.0, "size_source": "drop_low_conf",
                "classifier_features_json": features_json,
                "classifier_llm_response_json": llm_json,
                "classifier_reason": reason,
            }
            ctx.update(updates)
            return SkillResult(status="success", updates=updates,
                               reason=f"low_confidence:{confidence:.2f}")

        if confidence < HIGH_CONF_THRESHOLD:
            final_bucket = "LOW"
            size_pct = SIZE_LOW
            size_source = "downgrade"
        else:
            final_bucket = bucket
            size_pct = SIZE_HIGH if bucket == "HIGH" else SIZE_LOW
            size_source = "bucket_high" if bucket == "HIGH" else "bucket_low"

        updates = {
            "ticker": ticker, "side": side,
            "bucket": final_bucket, "confidence": confidence,
            "size_pct": size_pct, "size_source": size_source,
            "classifier_features_json": features_json,
            "classifier_llm_response_json": llm_json,
            "classifier_reason": reason,
        }
        ctx.update(updates)
        return SkillResult(status="success", updates=updates)
