import pytest
import json
import aiosqlite
from agent.context import Context
from infra.storage.db import SCHEMA
from infra.storage.classification_log_store import ClassificationLogStore
from skills.signal.classification_logger import ClassificationLogger


@pytest.mark.asyncio
async def test_logger_records_dropped_low_conf_action():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()

        store = ClassificationLogStore(conn)
        logger = ClassificationLogger(store)
        ctx = Context(trace_id="t", event_id="e", data={
            "trader_handle": "wse",
            "trader_auto_execute": True,
            "full_message_text": "ambiguous msg",
            "bucket": "LOW",
            "confidence": 0.3,
            "size_pct": 0.0,
            "size_source": "drop_low_conf",
            "classifier_features_json": "{}",
            "classifier_llm_response_json": None,
            "classifier_reason": "very ambiguous",
        })
        result = await logger.run(ctx)
        assert result.status == "success"
        rows = await store.recent_for_trader("wse")
        assert rows[0]["action_taken"] == "dropped_low_conf"


@pytest.mark.asyncio
async def test_logger_records_skipped_action():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()

        store = ClassificationLogStore(conn)
        logger = ClassificationLogger(store)
        ctx = Context(trace_id="t", event_id="e", data={
            "trader_handle": "wse",
            "trader_auto_execute": True,
            "full_message_text": "macro commentary",
            "bucket": "SKIP",
            "confidence": 0.95,
            "size_pct": 0.0,
            "size_source": "skip",
            "classifier_features_json": "{}",
            "classifier_llm_response_json": None,
            "classifier_reason": "commentary",
        })
        await logger.run(ctx)
        rows = await store.recent_for_trader("wse")
        assert rows[0]["action_taken"] == "skipped"


@pytest.mark.asyncio
async def test_logger_records_fired_action_for_autonomous():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()

        store = ClassificationLogStore(conn)
        logger = ClassificationLogger(store)
        ctx = Context(trace_id="t", event_id="e", data={
            "trader_handle": "wse",
            "trader_auto_execute": True,
            "full_message_text": "Added 2% AUDC",
            "bucket": "LOW",
            "confidence": 1.0,
            "size_pct": 0.02,
            "size_source": "shortcut_stated",
            "classifier_features_json": "{}",
            "classifier_llm_response_json": None,
            "classifier_reason": "stated_size_in_message",
        })
        await logger.run(ctx)
        rows = await store.recent_for_trader("wse")
        assert rows[0]["action_taken"] == "fired"




@pytest.mark.asyncio
async def test_logger_records_llm_error_action():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()

        store = ClassificationLogStore(conn)
        logger = ClassificationLogger(store)
        ctx = Context(trace_id="t", event_id="e", data={
            "trader_handle": "wse",
            "trader_auto_execute": True,
            "full_message_text": "msg",
            "bucket": "SKIP",
            "confidence": 0.0,
            "size_pct": 0.0,
            "size_source": "llm_error",
            "classifier_features_json": "{}",
            "classifier_llm_response_json": None,
            "classifier_reason": "llm_error:TimeoutError",
        })
        await logger.run(ctx)
        rows = await store.recent_for_trader("wse")
        assert rows[0]["action_taken"] == "llm_error"


@pytest.mark.asyncio
async def test_logger_records_fired_for_low_bucket_without_size_pct():
    """After classifier rework, size_pct is no longer set for HIGH/LOW signals.
    The logger must use bucket alone (not size_pct) to determine action_taken."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        store = ClassificationLogStore(conn)
        logger = ClassificationLogger(store)
        ctx = Context(trace_id="t", event_id="e", data={
            "trader_handle": "wse",
            "trader_auto_execute": True,
            "full_message_text": "msg",
            "bucket": "LOW",
            "confidence": 0.9,
            # size_pct intentionally absent — this is the new normal post-Task 6
            "size_source": "shortcut_stated",
            "classifier_features_json": "{}",
            "classifier_llm_response_json": None,
            "classifier_reason": "stated_size_in_message",
        })
        await logger.run(ctx)
        rows = await store.recent_for_trader("wse")
        assert rows[0]["action_taken"] == "fired"
