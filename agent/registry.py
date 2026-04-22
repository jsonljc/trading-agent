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


def build_phase2b_execution_chain(policy, execution_store, gateway) -> list:
    from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
    from skills.execution.chain_lookup import ChainLookup
    from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
    from skills.execution.contract_selector import ContractSelector
    from skills.execution.order_sizer import OrderSizer
    from skills.execution.order_pricer import OrderPricer
    from skills.execution.order_submitter import OrderSubmitter
    from skills.execution.fill_waiter import FillWaiter

    return [
        ExecutionEligibilityGuard(policy),
        ChainLookup(gateway, execution_store._conn),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        OrderSubmitter(gateway, execution_store),
        FillWaiter(gateway, execution_store,
                   timeout=policy.execution.fill_wait_timeout_seconds),
    ]
