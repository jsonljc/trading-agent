import pytest
from agent.context import Context
from agent.traders.profile import TraderProfile
from agent.traders.registry import TraderRegistry
from skills.signal.sell_classifier import SellClassifier


def _profile(handle="mystic"):
    return TraderProfile(
        handle=handle, display_name="Mystic", discord_author_pattern="Mystic",
        alert_mention="@m", require_alert_mention=True, bot_authors_to_skip=(),
        auto_execute=True, size_in_message=False, prefer_message_size=True,
        classifier_model="claude-haiku-4-5", availability_phrases=(),
        conviction_examples=(),
    )


class FakeLLM:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def classify(self, *, system, model, messages):
        self.calls.append({"system": system, "model": model, "messages": messages})
        return self._response


def _ctx(text, *, bucket="SKIP", handle="mystic"):
    c = Context(trace_id="t", event_id="e")
    c.update({"trader_handle": handle, "full_message_text": text, "bucket": bucket})
    return c


def _sub(response, handle="mystic"):
    return SellClassifier(TraderRegistry([_profile(handle)]), FakeLLM(response))


@pytest.mark.asyncio
async def test_partial_sell_sets_action_and_scope():
    llm_resp = {"is_sell": True, "ticker": "AAPL", "scope": "partial",
                "fraction": 0.5, "confidence": 0.9, "reason": "trim"}
    sub = _sub(llm_resp)
    ctx = _ctx("sold half my AAPL")
    result = await sub.run(ctx)
    assert result.status == "success"
    assert ctx.get("action") == "sell"
    assert ctx.get("sell_ticker") == "AAPL"
    assert ctx.get("sell_scope") == "partial"
    assert ctx.get("sell_fraction") == 0.5


@pytest.mark.asyncio
async def test_full_exit_fraction_is_one():
    llm_resp = {"is_sell": True, "ticker": "NVDA", "scope": "full",
                "fraction": None, "confidence": 0.95, "reason": "closed"}
    ctx = _ctx("out of NVDA")
    await _sub(llm_resp).run(ctx)
    assert ctx.get("action") == "sell"
    assert ctx.get("sell_scope") == "full"
    assert ctx.get("sell_fraction") == 1.0


@pytest.mark.asyncio
async def test_partial_without_fraction_defaults_half():
    llm_resp = {"is_sell": True, "ticker": "NVDA", "scope": "partial",
                "fraction": None, "confidence": 0.95, "reason": "trim"}
    ctx = _ctx("trimmed NVDA")
    await _sub(llm_resp).run(ctx)
    assert ctx.get("sell_fraction") == 0.5


@pytest.mark.asyncio
async def test_no_exit_verb_is_noop_and_skips_llm():
    llm = FakeLLM({"is_sell": True, "ticker": "AAPL", "scope": "full",
                   "fraction": None, "confidence": 0.99, "reason": "x"})
    sub = SellClassifier(TraderRegistry([_profile()]), llm)
    ctx = _ctx("watching AAPL closely")  # no exit verb
    result = await sub.run(ctx)
    assert result.status == "success"
    assert ctx.get("action") is None
    assert llm.calls == []  # prefiltered, no LLM call


@pytest.mark.asyncio
async def test_actionable_entry_wins_no_sell(  ):
    # Mixed message already classified as an entry -> entry wins, sell dropped.
    llm = FakeLLM({"is_sell": True, "ticker": "TSLA", "scope": "full",
                   "fraction": None, "confidence": 0.99, "reason": "x"})
    sub = SellClassifier(TraderRegistry([_profile()]), llm)
    ctx = _ctx("out of AAPL, opened TSLA", bucket="HIGH")
    await sub.run(ctx)
    assert ctx.get("action") is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_low_confidence_is_failclosed_noop():
    llm_resp = {"is_sell": True, "ticker": "AAPL", "scope": "full",
                "fraction": None, "confidence": 0.4, "reason": "maybe"}
    ctx = _ctx("might be out of AAPL")
    await _sub(llm_resp).run(ctx)
    assert ctx.get("action") is None


@pytest.mark.asyncio
async def test_ticker_not_in_message_is_failclosed():
    # Anti-hallucination: LLM returns a ticker not present in the message text.
    llm_resp = {"is_sell": True, "ticker": "ZZZZ", "scope": "full",
                "fraction": None, "confidence": 0.99, "reason": "x"}
    ctx = _ctx("sold out of AAPL")
    await _sub(llm_resp).run(ctx)
    assert ctx.get("action") is None


@pytest.mark.asyncio
async def test_is_sell_false_is_noop():
    llm_resp = {"is_sell": False, "ticker": None, "scope": "full",
                "fraction": None, "confidence": 0.99, "reason": "commentary"}
    ctx = _ctx("AAPL sold off hard today on no news")  # 'sold' verb but commentary
    await _sub(llm_resp).run(ctx)
    assert ctx.get("action") is None
