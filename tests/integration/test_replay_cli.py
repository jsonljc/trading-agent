import importlib.util
import json
from pathlib import Path

import pytest

from infra.storage.db import get_connection
from infra.storage.signal_store import SignalStore
from infra.storage.classification_log_store import ClassificationLogStore

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = REPO_ROOT / "bin" / "replay.py"

ENTRY_MSG = ("starting a long position in $NVDA here, like the setup into the print "
             "@Stock Talk Weekly - Alerts")
SKIP_MSG = ("just some macro commentary, $SPY fading into the close, no position "
            "@Stock Talk Weekly - Alerts")
ENTRY_LLM = {"is_entry": True, "ticker": "NVDA", "side": "long",
             "bucket": "HIGH", "confidence": 0.95, "reason": "long"}
SKIP_LLM = {"is_entry": False, "ticker": None, "side": "none",
            "bucket": "SKIP", "confidence": 0.9, "reason": "macro"}


def _load_cli():
    spec = importlib.util.spec_from_file_location("replay_cli", BIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
                    reason="long", ticker="NVDA", side="long")
    await cl.insert(event_id="evt-skip", trader_handle="stocktalkweekly",
                    msg_text=SKIP_MSG, features={}, llm_response=SKIP_LLM,
                    bucket="SKIP", confidence=0.9, size_pct=0.0,
                    size_source="skip", action_taken="skipped",
                    reason="macro", ticker=None, side=None)
    await conn.commit()
    await conn.close()


def _digest(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_prints_table_exits_zero_and_is_read_only(tmp_path, capsys):
    import asyncio
    db_path = tmp_path / "live.db"
    asyncio.run(_seed(db_path))
    before = _digest(db_path)

    cli = _load_cli()
    rc = cli.main([
        "--db", str(db_path),
        "--policy", str(REPO_ROOT / "config" / "policy.yaml"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "evt-entry"[:8] in out or "evt-entr" in out
    assert "HIGH" in out
    assert "NVDA" in out
    # Summary line present.
    assert "success" in out.lower()
    # The live DB must be untouched (opened read-only).
    assert _digest(db_path) == before


def test_cli_json_output(tmp_path, capsys):
    import asyncio
    db_path = tmp_path / "live.db"
    asyncio.run(_seed(db_path))
    cli = _load_cli()
    rc = cli.main([
        "--db", str(db_path),
        "--policy", str(REPO_ROOT / "config" / "policy.yaml"),
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {r["event_id"]: r for r in payload["results"]}
    assert by_id["evt-entry"]["bucket"] == "HIGH"
    assert by_id["evt-entry"]["would_be_orders"][0]["action"] == "BUY"
    assert by_id["evt-skip"]["would_be_orders"] == []


def test_cli_missing_db_exits_2(tmp_path, capsys):
    cli = _load_cli()
    rc = cli.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2


def test_divergence_baseline_uses_latest_classification_row(tmp_path, capsys):
    """Two classification rows for the same event_id: an older bucket=LOW and a
    newer bucket=HIGH. The newer row must be the divergence baseline (the replay
    classifies the entry as HIGH, so HIGH baseline => NO divergence; a LOW
    baseline would wrongly diverge). This locks in the ORDER BY created_at, id
    determinism fix."""
    import asyncio

    async def _seed_dup(db_path):
        conn = await get_connection(str(db_path))
        sig = SignalStore(conn)
        cl = ClassificationLogStore(conn)
        await sig.insert({
            "id": "evt-entry", "source": "discord", "channel": "stocktalkweekly",
            "author": "Stock Talk Weekly", "trigger_preview": ENTRY_MSG,
            "full_message_text": ENTRY_MSG, "capture_mode": "extension",
            "message_fingerprint": "fp1", "received_at": "2026-05-15T14:30:00+00:00",
        })
        # Older row: bucket LOW. Newer row: bucket HIGH. created_at is set by the
        # store at insert time, so insertion order == chronological order.
        await cl.insert(event_id="evt-entry", trader_handle="stocktalkweekly",
                        msg_text=ENTRY_MSG, features={}, llm_response=ENTRY_LLM,
                        bucket="LOW", confidence=0.5, size_pct=0.01,
                        size_source="bucket_low", action_taken="fired",
                        reason="older", ticker="NVDA", side="long")
        await cl.insert(event_id="evt-entry", trader_handle="stocktalkweekly",
                        msg_text=ENTRY_MSG, features={}, llm_response=ENTRY_LLM,
                        bucket="HIGH", confidence=0.95, size_pct=0.05,
                        size_source="bucket_high", action_taken="fired",
                        reason="newer", ticker="NVDA", side="long")
        await conn.commit()
        await conn.close()

    db_path = tmp_path / "live.db"
    asyncio.run(_seed_dup(db_path))
    cli = _load_cli()
    rc = cli.main([
        "--db", str(db_path),
        "--policy", str(REPO_ROOT / "config" / "policy.yaml"),
        "--event-id", "evt-entry", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    res = payload["results"][0]
    assert res["bucket"] == "HIGH"
    # Newer (HIGH) row is the baseline => matches replayed HIGH => no divergence.
    assert res["divergence"] is None
    assert payload["summary"]["divergences"] == 0


def test_cli_event_id_filter(tmp_path, capsys):
    import asyncio
    db_path = tmp_path / "live.db"
    asyncio.run(_seed(db_path))
    cli = _load_cli()
    rc = cli.main([
        "--db", str(db_path),
        "--policy", str(REPO_ROOT / "config" / "policy.yaml"),
        "--event-id", "evt-entry", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["event_id"] == "evt-entry"
