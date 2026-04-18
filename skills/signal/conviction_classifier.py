import json
import re
import anthropic
from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel

_SYSTEM_PROMPT = """Classify trading message conviction as 'high' or 'low'.

HIGH conviction signals: "high conviction", "strongly drawn", "initiate a position", "enough data", "full size".
LOW conviction signals: "starting a position", "small starter", "watching closely", "nibbling", "first tranche".
When uncertain, default to 'low'.

Respond with JSON only:
{"conviction_bucket": "high" or "low", "reason": "one sentence"}"""


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


class ConvictionClassifier(Skill):
    name = "conviction_classifier"

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
        if not parsed or "conviction_bucket" not in parsed:
            return SkillResult(status="fail", reason=f"conviction_classifier parse error: {response.content[0].text[:100]}")
        bucket = parsed["conviction_bucket"]

        sizing = self._policy.sizing_policy
        pct = sizing.high_conviction_pct if bucket == "high" else sizing.low_conviction_pct

        return SkillResult(
            status="success",
            updates={"conviction_bucket": bucket, "target_allocation_pct": pct},
        )
