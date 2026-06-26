"""Tests for surfacing DEGRADED/MISSED entry-skips to the operator.

The system loudly alerts on broker-down skips but otherwise *silently* drops
signals in the cases below, which "probably missed a real entry":
  - classifier llm_error          (size_source="llm_error")
  - low-confidence drop           (size_source="drop_low_conf")
  - anti-hallucination ticker drop (size_source="ticker_not_in_msg")
  - unknown author / tracked chan (reason "no_trader_profile:<author>")
  - premarket/after-hours entry   (reason "entry_outside_rth:<session>")

CRITICAL: a *genuine* commentary SKIP (the LLM correctly classifying
chatter/news as not-an-entry, size_source="skip") must stay SILENT — alerting
on it would be spam.

These exercise the pure predicate `TelegramDigest.is_missed_entry_skip` and the
reason formatter `TelegramDigest.missed_entry_reason` that `main.on_skip`
delegates to, plus the end-to-end send via the real `send_missed_signal_alert`.
"""
import pytest
from agent.context import Context
from skills.posttrade.telegram_digest import TelegramDigest


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent.append(text)


def make_ctx(**kwargs) -> Context:
    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({
        "channel": "mystic", "author": "Mystic",
        "full_message_text": "Long $AVEX today",
        **kwargs,
    })
    return ctx


# The exact skip reason EntrySkipGate emits for every bucket=SKIP halt — the
# classifier-degraded cases are indistinguishable on `reason` alone, so the
# predicate must discriminate on ctx["size_source"].
ENTRY_SKIP_REASON = "no_entry:bucket=SKIP"


# ---------------------------------------------------------------------------
# is_missed_entry_skip — the DEGRADED/MISSED cases (must alert)
# ---------------------------------------------------------------------------

def test_llm_error_is_missed_entry():
    ctx = make_ctx(bucket="SKIP", size_source="llm_error",
                   classifier_reason="llm_error:TimeoutError")
    assert TelegramDigest.is_missed_entry_skip(ctx, ENTRY_SKIP_REASON)


def test_drop_low_conf_is_missed_entry():
    ctx = make_ctx(bucket="SKIP", size_source="drop_low_conf",
                   classifier_reason="ambiguous mention", confidence=0.40)
    assert TelegramDigest.is_missed_entry_skip(ctx, ENTRY_SKIP_REASON)


def test_ticker_not_in_msg_is_missed_entry():
    ctx = make_ctx(bucket="SKIP", size_source="ticker_not_in_msg",
                   classifier_reason="llm_ticker_not_in_msg:NVDA")
    assert TelegramDigest.is_missed_entry_skip(ctx, ENTRY_SKIP_REASON)


def test_no_trader_profile_is_missed_entry():
    # TraderRouter halts BEFORE the classifier runs → no size_source; key on the
    # reason string it emits.
    ctx = make_ctx(author="SomeNewGuy")
    assert TelegramDigest.is_missed_entry_skip(
        ctx, "no_trader_profile:SomeNewGuy")


def test_entry_outside_rth_is_missed_entry():
    # RthEntryGuard halts an actionable (HIGH/LOW) entry that fired off-session.
    ctx = make_ctx(bucket="HIGH", size_source="bucket_high")
    assert TelegramDigest.is_missed_entry_skip(
        ctx, "entry_outside_rth:premarket")


# ---------------------------------------------------------------------------
# is_missed_entry_skip — the SILENT cases (must NOT alert / no spam)
# ---------------------------------------------------------------------------

def test_genuine_commentary_skip_is_silent():
    # THE critical case: the LLM correctly classified chatter as not-an-entry.
    ctx = make_ctx(bucket="SKIP", size_source="skip",
                   classifier_reason="macro commentary, no position")
    assert not TelegramDigest.is_missed_entry_skip(ctx, ENTRY_SKIP_REASON)


def test_bot_author_skip_is_silent():
    ctx = make_ctx(author="MEE6")
    assert not TelegramDigest.is_missed_entry_skip(ctx, "bot_author:MEE6")


def test_missing_alert_mention_skip_is_silent():
    ctx = make_ctx()
    assert not TelegramDigest.is_missed_entry_skip(
        ctx, "missing_alert_mention:@everyone")


def test_unrelated_skip_reason_is_silent():
    ctx = make_ctx(bucket="HIGH", size_source="bucket_high")
    assert not TelegramDigest.is_missed_entry_skip(ctx, "same_day_duplicate:AVEX")


# ---------------------------------------------------------------------------
# missed_entry_reason — operator-facing detail
# ---------------------------------------------------------------------------

def test_missed_entry_reason_surfaces_classifier_detail():
    # For classifier drops the raw skip reason is the useless "no_entry:bucket=SKIP";
    # surface the size_source + classifier_reason so the operator sees WHY.
    ctx = make_ctx(bucket="SKIP", size_source="llm_error",
                   classifier_reason="llm_error:TimeoutError")
    detail = TelegramDigest.missed_entry_reason(ctx, ENTRY_SKIP_REASON)
    assert "llm_error" in detail
    assert "TimeoutError" in detail
    assert detail != ENTRY_SKIP_REASON


def test_missed_entry_reason_passes_router_reason_through():
    ctx = make_ctx(author="SomeNewGuy")
    detail = TelegramDigest.missed_entry_reason(ctx, "no_trader_profile:SomeNewGuy")
    assert detail == "no_trader_profile:SomeNewGuy"


# ---------------------------------------------------------------------------
# End-to-end: mirrors main.on_skip's missed-entry branch
#   elif TelegramDigest.is_missed_entry_skip(ctx, reason):
#       await digest_skill.send_missed_signal_alert(
#           ctx, TelegramDigest.missed_entry_reason(ctx, reason))
# ---------------------------------------------------------------------------

async def _simulate_on_skip(skill: TelegramDigest, ctx: Context, reason: str) -> bool:
    if TelegramDigest.is_missed_entry_skip(ctx, reason):
        await skill.send_missed_signal_alert(
            ctx, TelegramDigest.missed_entry_reason(ctx, reason))
        return True
    return False


async def test_llm_error_triggers_missed_signal_alert():
    client = FakeTelegramClient()
    skill = TelegramDigest(client)
    ctx = make_ctx(bucket="SKIP", ticker="AVEX", side="long",
                   trader_handle="mystic", size_source="llm_error",
                   classifier_reason="llm_error:TimeoutError")
    alerted = await _simulate_on_skip(skill, ctx, ENTRY_SKIP_REASON)
    assert alerted
    assert len(client.sent) == 1
    msg = client.sent[0]
    assert "MISSED SIGNAL" in msg
    assert "llm_error" in msg


async def test_no_trader_profile_triggers_missed_signal_alert():
    client = FakeTelegramClient()
    skill = TelegramDigest(client)
    ctx = make_ctx(author="SomeNewGuy")
    alerted = await _simulate_on_skip(skill, ctx, "no_trader_profile:SomeNewGuy")
    assert alerted
    assert len(client.sent) == 1
    assert "MISSED SIGNAL" in client.sent[0]
    assert "no_trader_profile" in client.sent[0]


async def test_genuine_commentary_skip_sends_no_alert():
    client = FakeTelegramClient()
    skill = TelegramDigest(client)
    ctx = make_ctx(bucket="SKIP", size_source="skip",
                   classifier_reason="macro commentary, no position")
    alerted = await _simulate_on_skip(skill, ctx, ENTRY_SKIP_REASON)
    assert not alerted
    assert client.sent == []
