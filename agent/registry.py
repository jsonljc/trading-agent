from agent.skill import Skill


def build_phase1_chain(policy, idempotency_store, telegram_client) -> list[Skill]:
    """Returns the Phase 1 skill chain. Populated incrementally as skills are built."""
    from skills.signal.message_normalizer import MessageNormalizer
    from skills.signal.desktop_reader import DesktopReader
    from skills.signal.trade_intent_detector import TradeIntentDetector
    from skills.risk.idempotency_check import IdempotencyCheck
    from skills.signal.ticker_resolver import TickerResolver
    from skills.signal.conviction_classifier import ConvictionClassifier
    from skills.posttrade.telegram_digest import TelegramDigest

    return [
        MessageNormalizer(policy),
        DesktopReader(policy),
        TradeIntentDetector(policy),
        IdempotencyCheck(policy, idempotency_store),
        TickerResolver(policy),
        ConvictionClassifier(policy),
        TelegramDigest(telegram_client, mode="signal_only"),
    ]
