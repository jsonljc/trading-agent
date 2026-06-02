from __future__ import annotations
import json
import logging

from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.traders.registry import TraderRegistry
from skills.signal.feature_extractor import extract_features
from skills.signal.trader_classifier import LLMClassifierClient

logger = logging.getLogger(__name__)

SELL_CONF_THRESHOLD = 0.70

_SELL_PREAMBLE = """You decide whether a single trader's Discord message is an EXPLICIT SELL/EXIT of a position they hold, and how much they sold.

A SELL is a clear, first-person disposal: "sold", "out of X", "closed", "trimmed", "taking profits", "scaling out", "stopped out". It is NOT: commentary that a stock "sold off", news, watchlist removals, "no position", or hypotheticals.

Scope:
- "full": closed the entire position ("out", "closed", "sold the rest", "done with it").
- "partial": reduced but not closed ("trimmed", "sold half", "sold 1/3", "scaling out"). If a fraction is stated, return it (0.5 for half, 0.33 for a third); else null.

Return JSON only:
{"is_sell": bool, "ticker": "SYMBOL"|null, "scope": "full"|"partial", "fraction": 0.0-1.0|null, "confidence": 0.0-1.0, "reason": "one sentence"}

Examples for THIS specific trader:
"""


def _build_sell_prompt(profile) -> str:
    block = "\n".join(
        f"- MSG: {e.msg!r}\n  SCOPE: {e.scope}\n  WHY: {e.why}"
        for e in profile.sell_examples
    )
    return _SELL_PREAMBLE + (block or "(none provided)")


class SellClassifier(Skill):
    """Detects an explicit trader SELL and extracts {ticker, scope, fraction}.

    Self-gating and fail-closed: no-ops unless an exit verb is present AND the
    entry classifier did not already produce an actionable entry (entries win
    mixed messages). On a confident sell it sets ctx.action='sell' + sell_* keys;
    SellFollower (next in the chain) acts on them. It never touches `bucket`.
    """

    name = "SellClassifier"

    def __init__(self, registry: TraderRegistry, llm: LLMClassifierClient) -> None:
        self._registry = registry
        self._llm = llm

    async def run(self, ctx: Context) -> SkillResult:
        # Entry wins a mixed entry+exit message (documented v1 policy).
        if ctx.get("bucket") in ("HIGH", "LOW"):
            return SkillResult(status="success")

        msg = ctx.get("full_message_text", "")
        features = extract_features(msg)
        if not features.exit_verb_present:
            return SkillResult(status="success")  # cheap prefilter; no LLM call

        handle = ctx.get("trader_handle")
        profile = next((p for p in self._registry.all() if p.handle == handle), None)
        if profile is None:
            return SkillResult(status="success")  # unknown trader -> let entry path decide

        try:
            response = await self._llm.classify(
                system=[{"type": "text", "text": _build_sell_prompt(profile),
                         "cache_control": {"type": "ephemeral"}}],
                model=profile.classifier_model,
                messages=[{"role": "user", "content": msg}],
            )
        except Exception as exc:
            logger.exception("sell_classifier llm error: %s", exc)
            return SkillResult(status="success")  # fail-closed: drop, don't sell

        if not response.get("is_sell"):
            return SkillResult(status="success")
        confidence = float(response.get("confidence", 0.0))
        if confidence < SELL_CONF_THRESHOLD:
            return SkillResult(status="success")

        # Anti-hallucination: the ticker must appear in the message (same guard
        # as the entry classifier) when the extractor found any tickers.
        ticker = (response.get("ticker") or "").upper().strip()
        tickers_upper = {t.upper() for t in features.tickers_in_msg}
        if not ticker or (tickers_upper and ticker not in tickers_upper):
            return SkillResult(status="success")

        scope = response.get("scope", "full")
        if scope == "full":
            fraction = 1.0
        else:
            scope = "partial"
            raw = response.get("fraction")
            fraction = float(raw) if raw else 0.5
            fraction = min(max(fraction, 0.01), 1.0)

        updates = {
            "action": "sell",
            "sell_ticker": ticker,
            "sell_scope": scope,
            "sell_fraction": fraction,
            "sell_confidence": confidence,
            "sell_reason": response.get("reason", ""),
            "sell_llm_response_json": json.dumps(response),
        }
        ctx.update(updates)
        logger.info("SellClassifier: %s %s scope=%s frac=%.2f conf=%.2f",
                    handle, ticker, scope, fraction, confidence)
        return SkillResult(status="success", updates=updates)
