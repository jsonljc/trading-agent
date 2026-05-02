import pytest
from pathlib import Path
from agent.traders.profile import TraderProfile, ConvictionExample, load_profile, load_all_profiles


YAML_TEXT = """
handle: testtrader
display_name: Test Trader
discord_author_pattern: "Test Trader"
alert_mention: "@Test - Alerts"
require_alert_mention: true
bot_authors_to_skip: []
auto_execute: true
size_in_message: false
prefer_message_size: true
classifier_model: claude-haiku-4-5
availability_phrases: ["off the grid"]
conviction_examples:
  - msg: "Added 2% TEST"
    bucket: LOW
    why: "explicit 2%"
  - msg: "watching TEST"
    bucket: SKIP
    why: "no entry"
"""


def test_load_profile_parses_all_fields(tmp_path: Path):
    p = tmp_path / "test.yaml"
    p.write_text(YAML_TEXT)
    profile = load_profile(p)
    assert profile.handle == "testtrader"
    assert profile.display_name == "Test Trader"
    assert profile.alert_mention == "@Test - Alerts"
    assert profile.require_alert_mention is True
    assert profile.auto_execute is True
    assert profile.classifier_model == "claude-haiku-4-5"
    assert profile.availability_phrases == ("off the grid",)
    assert len(profile.conviction_examples) == 2
    assert profile.conviction_examples[0] == ConvictionExample(
        msg="Added 2% TEST", bucket="LOW", why="explicit 2%"
    )


def test_load_profile_rejects_invalid_bucket(tmp_path: Path):
    bad_yaml = YAML_TEXT + "  - msg: bad\n    bucket: BANANA\n    why: test\n"
    p = tmp_path / "bad.yaml"
    p.write_text(bad_yaml)
    with pytest.raises(ValueError, match="invalid bucket 'BANANA'"):
        load_profile(p)


def test_load_profile_rejects_empty_yaml(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_profile(p)


def test_load_profile_rejects_missing_required_field(tmp_path: Path):
    p = tmp_path / "incomplete.yaml"
    p.write_text("display_name: only this\n")
    with pytest.raises(ValueError, match="missing required fields"):
        load_profile(p)


def test_load_all_profiles_reads_directory(tmp_path: Path):
    (tmp_path / "a.yaml").write_text(YAML_TEXT)
    (tmp_path / "b.yaml").write_text(YAML_TEXT.replace("testtrader", "second"))
    profiles = load_all_profiles(tmp_path)
    # Files are sorted alphabetically by filename, so a.yaml (testtrader) before b.yaml (second).
    assert [p.handle for p in profiles] == ["testtrader", "second"]
