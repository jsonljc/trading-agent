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


def test_signal_only_chain_has_no_sell_skills():
    # Without the execution deps (trade_intent_store/exits_store) the chain
    # stays entry-only.
    names = [s.name for s in _chain()]
    assert "SellClassifier" not in names
    assert "SellFollower" not in names


def test_sell_skills_wire_in_pinned_order_when_deps_present():
    policy = MagicMock()
    policy.execution.shares_slippage_cap_pct = 0.01
    policy.execution.fill_wait_timeout_seconds = 30.0
    chain = build_phase1_chain(
        policy, idempotency_store=MagicMock(), telegram_client=MagicMock(),
        gateway=MagicMock(), trader_registry=MagicMock(),
        classification_log_store=MagicMock(), llm_classifier=MagicMock(),
        trade_intent_store=MagicMock(), exits_store=MagicMock(),
    )
    names = [s.name for s in chain]
    # Pinned order: TraderClassifier -> SellClassifier -> ClassificationLogger
    #   -> SameDayDedupGate -> SellFollower -> EntrySkipGate
    assert names.index("TraderClassifier") < names.index("SellClassifier")
    assert names.index("SellClassifier") < names.index("ClassificationLogger")
    assert names.index("SameDayDedupGate") < names.index("SellFollower")
    assert names.index("SellFollower") < names.index("EntrySkipGate")
