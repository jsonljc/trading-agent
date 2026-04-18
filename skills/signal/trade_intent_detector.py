import json
import re
import logging
import anthropic
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You classify Discord trading messages into one of four intents.

LONG_SIGNAL: explicit statement that the author is going long / entering / initiating a position.
ADD_SIGNAL: adding to an existing long position.
WATCHLIST_ONLY: observing or monitoring, not yet acting.
NO_ACTION: commentary, news, analysis, or anything not actionable.

Action words that indicate LONG_SIGNAL or ADD_SIGNAL: long, initiating, starting position, adding, entered, bought.
Words that indicate WATCHLIST_ONLY: watching, monitoring, keeping an eye, interesting, on radar.

Respond with valid JSON only:
{"intent": "LONG_SIGNAL|ADD_SIGNAL|WATCHLIST_ONLY|NO_ACTION", "confidence": "high|medium|low", "reason": "one sentence"}"""


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


class TradeIntentDetector(Skill):
    name = "trade_intent_detector"

    def __init__(self, policy: PolicyModel) -> None:
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
        if not parsed or "intent" not in parsed:
            return SkillResult(status="fail", reason=f"intent detector parse error: {response.content[0].text[:100]}")

        intent = parsed["intent"]
        confidence = parsed.get("confidence", "medium")
        reason = parsed.get("reason", "")

        if intent in ("NO_ACTION", "WATCHLIST_ONLY"):
            return SkillResult(status="skip", reason=f"{intent}: {reason}")

        return SkillResult(
            status="success",
            updates={"intent": intent, "confidence": confidence, "reason": reason},
        )
