import json
from pathlib import Path

import pytest

import importlib.util

# Load bin/eval_classifiers.py as a module (it is a script, not a package).
_BIN = Path(__file__).resolve().parents[2] / "bin" / "eval_classifiers.py"
_spec = importlib.util.spec_from_file_location("eval_classifiers", _BIN)
eval_classifiers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_classifiers)
main = eval_classifiers.main
RecordedLLM = eval_classifiers.RecordedLLM

_ROOT = Path(__file__).resolve().parents[2]
TRADERS_DIR = str(_ROOT / "config" / "traders")
SHIPPED_FIXTURES = str(_ROOT / "tests" / "fixtures" / "classifier_eval")
SHIPPED_RESPONSES = str(
    _ROOT / "tests" / "fixtures" / "classifier_eval" / "responses_sample.jsonl")


def _write_tiny_fixtures(d: Path):
    (d / "entry.jsonl").write_text(
        '{"msg": "Added 10% pos $AAPL", "trader": "wallstengine", "kind": "entry", "expected": "HIGH"}\n'
        '{"msg": "thinking about $NET, no position", "trader": "wallstengine", "kind": "entry", "expected": "SKIP"}\n'
    )
    (d / "sell.jsonl").write_text(
        '{"msg": "out of $NVDA completely", "trader": "mystic", "kind": "sell", '
        '"expected": {"is_sell": true, "scope": "full"}}\n'
        '{"msg": "watching $MU, might add", "trader": "mystic", "kind": "sell", '
        '"expected": {"is_sell": false, "scope": null}}\n'
    )


def _write_responses(p: Path):
    # Only the messages that hit the LLM path need a recorded response.
    # "Added 10% pos $AAPL" hits the deterministic shortcut (no LLM).
    # "watching $MU, might add" has no exit verb (sell prefilter, no LLM).
    lines = [
        {"msg": "thinking about $NET, no position",
         "response": {"is_entry": False, "ticker": None, "side": "none",
                      "bucket": "SKIP", "confidence": 0.9, "reason": "no position"}},
        {"msg": "out of $NVDA completely",
         "response": {"is_sell": True, "ticker": "NVDA", "scope": "full",
                      "fraction": None, "confidence": 0.95, "reason": "closed"}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_recorded_llm_returns_canned_and_raises_on_miss():
    llm = RecordedLLM({"hello": {"bucket": "SKIP"}})
    import asyncio
    resp = asyncio.run(llm.classify(system=[], model="m",
                                    messages=[{"role": "user", "content": "hello"}]))
    assert resp == {"bucket": "SKIP"}
    with pytest.raises(KeyError):
        asyncio.run(llm.classify(system=[], model="m",
                                 messages=[{"role": "user", "content": "missing"}]))


def test_cli_prints_metrics_exits_zero(tmp_path, capsys):
    fx = tmp_path / "fx"
    fx.mkdir()
    _write_tiny_fixtures(fx)
    responses = tmp_path / "resp.jsonl"
    _write_responses(responses)

    rc = main([
        "--fixtures-dir", str(fx),
        "--traders", TRADERS_DIR,
        "--responses", str(responses),
        "--classifier", "both",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    # metrics surfaced
    assert "accuracy" in out.lower()
    assert "precision" in out.lower() or "f1" in out.lower()
    # both classifiers reported
    assert "entry" in out.lower()
    assert "sell" in out.lower()


def test_cli_json_mode_valid_json(tmp_path, capsys):
    fx = tmp_path / "fx"
    fx.mkdir()
    _write_tiny_fixtures(fx)
    responses = tmp_path / "resp.jsonl"
    _write_responses(responses)

    rc = main([
        "--fixtures-dir", str(fx),
        "--traders", TRADERS_DIR,
        "--responses", str(responses),
        "--classifier", "entry",
        "--json",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    # one or more report objects
    assert isinstance(data, (dict, list))


def test_cli_requires_llm_source(tmp_path, capsys):
    fx = tmp_path / "fx"
    fx.mkdir()
    _write_tiny_fixtures(fx)
    rc = main([
        "--fixtures-dir", str(fx),
        "--traders", TRADERS_DIR,
    ])  # no --responses, no --live-llm
    err = capsys.readouterr().err
    assert rc != 0
    assert "responses" in err.lower() or "live-llm" in err.lower()


def test_shipped_sample_cache_matches_shipped_fixtures(capsys):
    # Deterministic regression over the committed fixtures + committed sample
    # cache. The cache is an ideal oracle, so a complete + in-sync cache yields
    # 100% accuracy with no KeyError; this guards the cache against fixture
    # drift (a new fixture message with no recorded response would KeyError).
    rc = main([
        "--fixtures-dir", SHIPPED_FIXTURES,
        "--traders", TRADERS_DIR,
        "--responses", SHIPPED_RESPONSES,
        "--classifier", "both",
        "--json",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    for report in data["pooled"]:
        assert report["accuracy"] == 1.0, (
            f"{report['classifier']} not 100% on oracle cache: "
            f"{report['accuracy']}")


def test_cli_does_not_touch_live_llm(tmp_path, monkeypatch, capsys):
    fx = tmp_path / "fx"
    fx.mkdir()
    _write_tiny_fixtures(fx)
    responses = tmp_path / "resp.jsonl"
    _write_responses(responses)

    # Sabotage the live-LLM constructor: if the CLI ever instantiates it on the
    # recorded path the test fails loudly.
    def _boom(*a, **k):
        raise AssertionError("live LLM must not be constructed on recorded path")

    monkeypatch.setattr(eval_classifiers, "AnthropicClassifierClient", _boom)
    rc = main([
        "--fixtures-dir", str(fx),
        "--traders", TRADERS_DIR,
        "--responses", str(responses),
    ])
    assert rc == 0
