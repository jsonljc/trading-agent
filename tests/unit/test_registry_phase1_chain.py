from unittest.mock import MagicMock
from agent.registry import build_phase1_chain


def _chain():
    return build_phase1_chain(
        MagicMock(), idempotency_store=MagicMock(), telegram_client=MagicMock(),
        gateway=MagicMock(), trader_registry=MagicMock(),
        classification_log_store=MagicMock(), llm_classifier=MagicMock(),
    )


def test_phase1_chain_excludes_desktop_reader():
    names = [s.name for s in _chain()]
    assert "desktop_reader" not in names


def test_phase1_chain_keeps_core_skills_in_order():
    names = [s.name for s in _chain()]
    # ClassificationLogger must precede SameDayDedupGate (dedup reads its log row)
    assert names.index("ClassificationLogger") < names.index("SameDayDedupGate")
    for expected in ("message_normalizer", "TraderClassifier",
                     "SameDayDedupGate", "EntrySkipGate"):
        assert expected in names
