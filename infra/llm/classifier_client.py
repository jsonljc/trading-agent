from __future__ import annotations
import json
import re


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class AnthropicClassifierClient:
    def __init__(self, anthropic_client, max_tokens: int = 256) -> None:
        self._anth = anthropic_client
        self._max_tokens = max_tokens

    async def classify(self, *, system: list, model: str, messages: list) -> dict:
        response = await self._anth.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
        )
        text = response.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = _JSON_OBJECT.search(text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        raise ValueError(f"classifier_response_parse_error: {text[:200]}")
