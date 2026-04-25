from __future__ import annotations
import json
import re
import logging
import anthropic
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)

_VALID_SIDES = {"long", "short", "none"}
_VALID_CONVICTIONS = {"high", "medium", "low"}
_VALID_FLAGS = {
    "ticker_implicit", "multiple_tickers_detected", "direction_unclear",
    "non_actionable_commentary", "slang_interpretation",
}

_SYSTEM_PROMPT = """You analyze Discord trading messages and return a JSON object. Return JSON only — no prose.

Required fields:
- is_trade_signal: boolean — true only if the author is entering/adding to a position
- ticker: string or null — the stock ticker symbol
- side: "long" | "short" | "none"
- conviction: "high" | "medium" | "low"
- analysis_confidence: float 0.0–1.0 — your confidence in the extraction
- ambiguity_flags: array — zero or more of: ticker_implicit, multiple_tickers_detected, direction_unclear, non_actionable_commentary, slang_interpretation
- rationale: string — one sentence explaining the classification

Set is_trade_signal=false for commentary, news, watchlist mentions, and analysis without position entry."""


def _safe_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


class SignalAnalyzer(Skill):
    name = "SignalAnalyzer"

    def __init__(self, policy) -> None:
        self._policy = policy
        self._client = anthropic.AsyncAnthropic()

    async def run(self, ctx: Context) -> SkillResult:
        text = ctx.get("full_message_text", "")
        channel = ctx.get("channel", "")
        author = ctx.get("author", "")

        response = await self._client.messages.create(
            model=self._policy.models.text,
            max_tokens=256,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"Channel: #{channel}\nAuthor: {author}\nMessage: {text}"}],
        )

        parsed = _safe_json(response.content[0].text)
        if not parsed or "is_trade_signal" not in parsed:
            return SkillResult(status="fail", reason="signal_parse_failed: could not parse SignalAnalyzer response")

        side = parsed.get("side", "none")
        conviction = parsed.get("conviction", "low")
        flags = parsed.get("ambiguity_flags", [])

        if side not in _VALID_SIDES:
            return SkillResult(status="fail", reason=f"signal_parse_failed: invalid side '{side}'")
        if conviction not in _VALID_CONVICTIONS:
            return SkillResult(status="fail", reason=f"signal_parse_failed: invalid conviction '{conviction}'")
        for flag in flags:
            if flag not in _VALID_FLAGS:
                return SkillResult(status="fail", reason=f"signal_parse_failed: unknown flag '{flag}'")

        if not parsed.get("is_trade_signal"):
            return SkillResult(status="skip", reason=f"not_a_trade_signal: {parsed.get('rationale', '')}")

        confidence = float(parsed.get("analysis_confidence", 0.0))
        if confidence < 0.70 or flags:
            return SkillResult(
                status="skip",
                reason=f"ambiguous_signal: confidence={confidence:.2f} flags={flags}",
            )

        ticker = (parsed.get("ticker") or "").upper().strip()
        if not ticker:
            return SkillResult(status="fail", reason="signal_parse_failed: ticker is null")

        return SkillResult(status="success", updates={
            "ticker": ticker,
            "ticker_raw": parsed.get("ticker"),
            "side": side,
            "side_raw": side,
            "conviction": conviction,
            "conviction_raw": conviction,
            "analysis_confidence": confidence,
            "ambiguity_flags": json.dumps(flags),
            "rationale": parsed.get("rationale", ""),
            "intent": "LONG_SIGNAL" if side == "long" else "SHORT_SIGNAL",
            "conviction_bucket": conviction,
        })
