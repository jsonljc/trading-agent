import json
from pathlib import Path

import pytest

from agent.policy import load_policy
from agent.replay.recorded_llm import RecordedClassifierClient
from agent.replay.runner import replay_all
from infra.storage.db import get_connection
from infra.storage.signal_store import SignalStore
from infra.storage.classification_log_store import ClassificationLogStore

REPO_ROOT = Path(__file__).resolve().parents[2]

ENTRY_MSG = ("starting a long position in $NVDA here, like the setup into the print "
             "@Stock Talk Weekly - Alerts")
SKIP_MSG = ("just some macro commentary, $SPY $QQQ fading into the close, no position "
            "@Stock Talk Weekly - Alerts")

ENTRY_LLM = {"is_entry": True, "ticker": "NVDA", "side": "long",
             "bucket": "HIGH", "confidence": 0.95, "reason": "clear long entry"}
SKIP_LLM = {"is_entry": False, "ticker": None, "side": "none",
            "bucket": "SKIP", "confidence": 0.9, "reason": "macro commentary"}


async def _seed(db_path):
    conn = await get_connection(str(db_path))
    sig = SignalStore(conn)
    cl = ClassificationLogStore(conn)
    await sig.insert({
        "id": "evt-entry", "source": "discord", "channel": "stocktalkweekly",
        "author": "Stock Talk Weekly", "trigger_preview": ENTRY_MSG,
        "full_message_text": ENTRY_MSG, "capture_mode": "extension",
        "message_fingerprint": "fp1", "received_at": "2026-05-15T14:30:00+00:00",
    })
    await sig.insert({
        "id": "evt-skip", "source": "discord", "channel": "stocktalkweekly",
        "author": "Stock Talk Weekly", "trigger_preview": SKIP_MSG,
        "full_message_text": SKIP_MSG, "capture_mode": "extension",
        "message_fingerprint": "fp2", "received_at": "2026-05-15T14:31:00+00:00",
    })
    await cl.insert(event_id="evt-entry", trader_handle="stocktalkweekly",
                    msg_text=ENTRY_MSG, features={}, llm_response=ENTRY_LLM,
                    bucket="HIGH", confidence=0.95, size_pct=0.05,
                    size_source="bucket_high", action_taken="fired",
                    reason="clear long entry", ticker="NVDA", side="long")
    await cl.insert(event_id="evt-skip", trader_handle="stocktalkweekly",
                    msg_text=SKIP_MSG, features={}, llm_response=SKIP_LLM,
                    bucket="SKIP", confidence=0.9, size_pct=0.0,
                    size_source="skip", action_taken="skipped",
                    reason="macro commentary", ticker=None, side=None)
    await conn.commit()
    await conn.close()


@pytest.mark.asyncio
async def test_replay_all_entry_and_skip(tmp_path):
    db_path = tmp_path / "seed.db"
    await _seed(db_path)

    policy = load_policy(str(REPO_ROOT / "config" / "policy.yaml"))
    recorded = RecordedClassifierClient({ENTRY_MSG: ENTRY_LLM, SKIP_MSG: SKIP_LLM})

    rows = [
        {"id": "evt-entry", "channel": "stocktalkweekly",
         "author": "Stock Talk Weekly", "full_message_text": ENTRY_MSG,
         "trigger_preview": ENTRY_MSG, "received_at": "2026-05-15T14:30:00+00:00"},
        {"id": "evt-skip", "channel": "stocktalkweekly",
         "author": "Stock Talk Weekly", "full_message_text": SKIP_MSG,
         "trigger_preview": SKIP_MSG, "received_at": "2026-05-15T14:31:00+00:00"},
    ]
    results = await replay_all(rows, policy, recorded, net_liq=100_000.0, quote=100.0)

    by_id = {r.event_id: r for r in results}

    entry = by_id["evt-entry"]
    assert entry.bucket == "HIGH"
    assert entry.ticker == "NVDA"
    assert entry.side == "long"
    assert entry.llm_recorded is True
    # A would-be BUY with qty > 0, and it is a recorded order (not a real one).
    assert len(entry.would_be_orders) == 1
    order = entry.would_be_orders[0]
    assert order["action"] == "BUY"
    assert order["quantity"] > 0
    assert order["instrument"] == "NVDA"
    assert entry.final_status == "success"

    skip = by_id["evt-skip"]
    assert skip.bucket == "SKIP"
    assert skip.would_be_orders == []
    assert skip.final_status == "skipped"
