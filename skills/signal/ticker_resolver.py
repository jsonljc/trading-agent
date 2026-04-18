import json
import re
import anthropic
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel

_SYSTEM_PROMPT = """Extract the stock ticker from a trading message.

Handle formats: $AVEX, ticker M-I-T-K, company names like "Mitek Systems".
If multiple tickers are present and no clear primary, set ambiguous=true.

Respond with JSON only:
{"ticker": "AVEX" or null, "ambiguous": false, "asset_type_hint": "equity"}"""


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


class TickerResolver(Skill):
    name = "ticker_resolver"

    def __init__(self, policy: PolicyModel) -> None:
        self._policy = policy
        self._client = anthropic.AsyncAnthropic()

    async def run(self, ctx: Context) -> SkillResult:
        text = ctx.get("full_message_text", "")

        response = await self._client.messages.create(
            model=self._policy.models.text,
            max_tokens=128,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text}],
        )

        parsed = _safe_json(response.content[0].text)
        if not parsed:
            return SkillResult(status="fail", reason=f"ticker_resolver parse error: {response.content[0].text[:100]}")

        if parsed.get("ambiguous") or not parsed.get("ticker"):
            return SkillResult(status="fail", reason="ambiguous ticker — cannot resolve to single symbol")

        return SkillResult(
            status="success",
            updates={"ticker": parsed["ticker"], "asset_type_hint": parsed.get("asset_type_hint", "equity")},
        )
