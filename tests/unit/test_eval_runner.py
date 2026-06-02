import pytest

from agent.eval_runner import Fixture, load_fixtures, run_entry, run_sell
from agent.traders.profile import ConvictionExample, SellExample, TraderProfile
from agent.traders.registry import TraderRegistry


def _entry_profile(handle="wallstengine", prefer_message_size=True, size_floor=None):
    return TraderProfile(
        handle=handle, display_name="Wall St Engine",
        discord_author_pattern="Wall St Engine", alert_mention="@x",
        require_alert_mention=True, bot_authors_to_skip=(), auto_execute=True,
        size_in_message=True, prefer_message_size=prefer_message_size,
        classifier_model="claude-haiku-4-5", availability_phrases=(),
        conviction_examples=(
            ConvictionExample(msg="x", bucket="HIGH", why="y"),
        ),
        size_floor=size_floor,
    )


def _sell_profile(handle="mystic"):
    return TraderProfile(
        handle=handle, display_name="Mystic", discord_author_pattern="Mystic",
        alert_mention="@m", require_alert_mention=True, bot_authors_to_skip=(),
        auto_execute=True, size_in_message=False, prefer_message_size=True,
        classifier_model="claude-haiku-4-5", availability_phrases=(),
        conviction_examples=(),
        sell_examples=(SellExample(msg="out of X", scope="full", why="z"),),
    )


class KeyedFakeLLM:
    """Returns a canned response keyed on the user message content."""

    def __init__(self, by_msg: dict[str, dict]):
        self._by_msg = by_msg
        self.calls: list[str] = []

    async def classify(self, *, system, model, messages):
        content = messages[0]["content"]
        self.calls.append(content)
        return self._by_msg[content]


# --- Fixture / load -------------------------------------------------------

def test_load_fixtures_parses_jsonl(tmp_path):
    p = tmp_path / "entry.jsonl"
    p.write_text(
        '{"msg": "buy AAPL", "trader": "wallstengine", "kind": "entry", "expected": "HIGH"}\n'
        '\n'  # blank line tolerated
        '{"msg": "out of NVDA", "trader": "mystic", "kind": "sell", '
        '"expected": {"is_sell": true, "scope": "full"}}\n'
    )
    fixtures = load_fixtures(p)
    assert len(fixtures) == 2
    assert fixtures[0] == Fixture(
        msg="buy AAPL", trader="wallstengine", kind="entry", expected="HIGH")
    assert fixtures[1].kind == "sell"
    assert fixtures[1].expected == {"is_sell": True, "scope": "full"}


# --- run_entry ------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_entry_shortcut_path_no_llm():
    # stated size + entry verb + single ticker -> deterministic shortcut, no LLM
    registry = TraderRegistry([_entry_profile()])
    llm = KeyedFakeLLM({})  # would KeyError if called
    fixtures = [
        Fixture(msg="Added 10% pos AAPL", trader="wallstengine",
                kind="entry", expected="HIGH"),
        Fixture(msg="Added 2% pos $XYZ", trader="wallstengine",
                kind="entry", expected="LOW"),
    ]
    pairs = await run_entry(fixtures, registry, llm)
    assert pairs == [("HIGH", "HIGH"), ("LOW", "LOW")]
    assert llm.calls == []


@pytest.mark.asyncio
async def test_run_entry_llm_path():
    registry = TraderRegistry([_entry_profile(prefer_message_size=False)])
    msg_high = "Alpha Omega long idea, deep multi-paragraph thesis AOSL"
    msg_skip = "watching the market, no position, fyi"
    llm = KeyedFakeLLM({
        msg_high: {"is_entry": True, "ticker": "AOSL", "side": "long",
                   "bucket": "HIGH", "confidence": 0.95, "reason": "thesis"},
        msg_skip: {"is_entry": False, "ticker": None, "side": "none",
                   "bucket": "SKIP", "confidence": 0.9, "reason": "commentary"},
    })
    fixtures = [
        Fixture(msg=msg_high, trader="wallstengine", kind="entry", expected="HIGH"),
        Fixture(msg=msg_skip, trader="wallstengine", kind="entry", expected="SKIP"),
    ]
    pairs = await run_entry(fixtures, registry, llm)
    assert pairs == [("HIGH", "HIGH"), ("SKIP", "SKIP")]
    assert set(llm.calls) == {msg_high, msg_skip}


@pytest.mark.asyncio
async def test_run_entry_default_skip_when_unknown_trader():
    registry = TraderRegistry([_entry_profile()])
    fixtures = [Fixture(msg="hello", trader="nobody", kind="entry", expected="SKIP")]
    pairs = await run_entry(fixtures, registry, KeyedFakeLLM({}))
    assert pairs == [("SKIP", "SKIP")]


# --- run_sell -------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_sell_full_partial_and_not_sell():
    registry = TraderRegistry([_sell_profile()])
    msg_full = "out of AAPL completely"
    msg_partial = "trimmed half my NVDA"
    msg_not = "watching AAPL, might add later"   # no exit verb -> not_sell
    msg_commentary = "AAPL sold off hard today on no news"  # exit verb, is_sell false
    llm = KeyedFakeLLM({
        msg_full: {"is_sell": True, "ticker": "AAPL", "scope": "full",
                   "fraction": None, "confidence": 0.95, "reason": "closed"},
        msg_partial: {"is_sell": True, "ticker": "NVDA", "scope": "partial",
                      "fraction": 0.5, "confidence": 0.9, "reason": "trim"},
        msg_commentary: {"is_sell": False, "ticker": None, "scope": "full",
                         "fraction": None, "confidence": 0.9, "reason": "news"},
    })
    fixtures = [
        Fixture(msg=msg_full, trader="mystic", kind="sell",
                expected={"is_sell": True, "scope": "full"}),
        Fixture(msg=msg_partial, trader="mystic", kind="sell",
                expected={"is_sell": True, "scope": "partial"}),
        Fixture(msg=msg_not, trader="mystic", kind="sell",
                expected={"is_sell": False, "scope": None}),
        Fixture(msg=msg_commentary, trader="mystic", kind="sell",
                expected={"is_sell": False, "scope": None}),
    ]
    is_sell_pairs, scope_pairs = await run_sell(fixtures, registry, llm)

    assert is_sell_pairs == [
        ("sell", "sell"),
        ("sell", "sell"),
        ("not_sell", "not_sell"),   # no exit verb prefilter
        ("not_sell", "not_sell"),   # is_sell false
    ]
    # scope pairs only collected where expected.is_sell is True
    assert scope_pairs == [("full", "full"), ("partial", "partial")]
    # the no-exit-verb message must not call the LLM
    assert msg_not not in llm.calls
