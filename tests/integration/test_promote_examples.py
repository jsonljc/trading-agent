import pytest
import yaml
from pathlib import Path
from infra.storage.examples_pending_store import ExamplesPendingStore
from bin.promote_examples import promote_one


@pytest.mark.asyncio
async def test_promote_appends_to_yaml_and_resolves_pending(db, tmp_path: Path):
    yaml_path = tmp_path / "wse.yaml"
    yaml_path.write_text(
        "handle: wallstengine\n"
        "display_name: Wall St Engine\n"
        "discord_author_pattern: \"Wall St Engine\"\n"
        "alert_mention: \"@Wall - Alerts\"\n"
        "require_alert_mention: true\n"
        "bot_authors_to_skip: []\n"
        "auto_execute: true\n"
        "size_in_message: true\n"
        "prefer_message_size: true\n"
        "classifier_model: claude-haiku-4-5\n"
        "availability_phrases: []\n"
        "conviction_examples:\n"
        "  - msg: existing\n"
        "    bucket: LOW\n"
        "    why: seed\n"
    )

    store = ExamplesPendingStore(db)
    pending_id = await store.insert(
        trader_handle="wallstengine", msg_text="brand new phrasing here",
        proposed_bucket="LOW", proposed_why="ambiguous low conf",
        source="low_confidence",
    )

    await promote_one(store, pending_id, yaml_path, approved_bucket="HIGH",
                      why_override="manual upgrade")

    data = yaml.safe_load(yaml_path.read_text())
    examples = data["conviction_examples"]
    assert len(examples) == 2
    assert examples[1] == {"msg": "brand new phrasing here", "bucket": "HIGH",
                           "why": "manual upgrade"}

    remaining = await store.list_pending(trader_handle="wallstengine")
    assert remaining == []


@pytest.mark.asyncio
async def test_promote_rejects_malformed_yaml_examples_field(db, tmp_path: Path):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text(
        "handle: x\n"
        "display_name: x\n"
        "discord_author_pattern: x\n"
        "alert_mention: \"@x\"\n"
        "conviction_examples: not_a_list\n"
    )
    store = ExamplesPendingStore(db)
    pending_id = await store.insert(
        trader_handle="x", msg_text="m", proposed_bucket="LOW",
        proposed_why="w", source="low_confidence",
    )
    with pytest.raises(SystemExit, match="not a list"):
        await promote_one(store, pending_id, yaml_path, approved_bucket="LOW")


@pytest.mark.asyncio
async def test_promote_creates_conviction_examples_when_missing(db, tmp_path: Path):
    yaml_path = tmp_path / "minimal.yaml"
    yaml_path.write_text(
        "handle: x\n"
        "display_name: x\n"
        "discord_author_pattern: x\n"
        "alert_mention: \"@x\"\n"
    )
    store = ExamplesPendingStore(db)
    pending_id = await store.insert(
        trader_handle="x", msg_text="hello", proposed_bucket="LOW",
        proposed_why="w", source="low_confidence",
    )
    await promote_one(store, pending_id, yaml_path, approved_bucket="LOW")
    data = yaml.safe_load(yaml_path.read_text())
    assert data["conviction_examples"] == [
        {"msg": "hello", "bucket": "LOW", "why": "w"}
    ]


@pytest.mark.asyncio
async def test_promote_atomic_write_no_temp_file_left(db, tmp_path: Path):
    yaml_path = tmp_path / "wse.yaml"
    yaml_path.write_text(
        "handle: x\n"
        "display_name: x\n"
        "discord_author_pattern: x\n"
        "alert_mention: \"@x\"\n"
        "conviction_examples: []\n"
    )
    store = ExamplesPendingStore(db)
    pending_id = await store.insert(
        trader_handle="x", msg_text="m", proposed_bucket="LOW",
        proposed_why="w", source="low_confidence",
    )
    await promote_one(store, pending_id, yaml_path, approved_bucket="LOW")
    # No leftover .tmp file from the atomic rename
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
