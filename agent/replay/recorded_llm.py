"""RecordedClassifierClient — replays the recorded LLM decision for an alert.

Same interface as infra.llm.classifier_client.AnthropicClassifierClient
(`async classify(*, system, model, messages) -> dict`). Constructed from a dict
mapping the user message text -> the recorded llm_response (parsed from
classification_log.llm_response_json). On a miss it returns a safe SKIP default
and counts the miss. Zero API calls — fully deterministic.
"""
from __future__ import annotations


_SKIP_DEFAULT = {"is_entry": False, "bucket": "SKIP", "confidence": 0.0}


class RecordedClassifierClient:
    def __init__(self, responses_by_text: dict[str, dict]) -> None:
        self._responses = dict(responses_by_text or {})
        self.hits = 0
        self.misses = 0

    def was_recorded(self, text: str) -> bool:
        return text in self._responses

    async def classify(self, *, system: list, model: str, messages: list) -> dict:
        text = self._user_text(messages)
        if text in self._responses:
            self.hits += 1
            return self._responses[text]
        self.misses += 1
        return dict(_SKIP_DEFAULT)

    @staticmethod
    def _user_text(messages: list) -> str:
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str):
                    return content
                # content can be a list of blocks; concatenate text blocks.
                if isinstance(content, list):
                    return "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict)
                    )
        return ""
