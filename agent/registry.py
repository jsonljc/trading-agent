from agent.skill import Skill


def build_phase1_chain(policy, idempotency_store, telegram_client, gateway=None,
                       trader_registry=None, classification_log_store=None,
                       llm_classifier=None) -> list:
    from skills.signal.message_normalizer import MessageNormalizer
    from skills.signal.desktop_reader import DesktopReader
    from skills.signal.trader_router import TraderRouter
    from skills.signal.trader_classifier import TraderClassifier
    from skills.signal.classification_logger import ClassificationLogger
    from skills.signal.entry_skip_gate import EntrySkipGate
    from skills.signal.bootstrap_review_gate import BootstrapReviewGate
    from skills.risk.idempotency_check import IdempotencyCheck
    from skills.posttrade.telegram_digest import TelegramDigest

    if trader_registry is None:
        raise ValueError("trader_registry is required for the conviction-classifier pipeline")
    if classification_log_store is None:
        raise ValueError("classification_log_store is required")
    if llm_classifier is None:
        raise ValueError("llm_classifier is required")

    digest = TelegramDigest(telegram_client, mode="signal_only")
    skills_list: list[Skill] = [
        MessageNormalizer(policy),
        DesktopReader(policy),
        TraderRouter(trader_registry),
        TraderClassifier(trader_registry, llm_classifier),
        ClassificationLogger(classification_log_store),
        EntrySkipGate(),                            # NEW: terminate non-actionable
        BootstrapReviewGate(digest),
        IdempotencyCheck(policy, idempotency_store),
    ]

    if gateway is not None:
        from skills.signal.ticker_validator import TickerValidator
        skills_list.append(TickerValidator(gateway))

    skills_list.append(digest)
    return skills_list


def build_phase2b_execution_chain(policy, execution_store, gateway,
                                   trade_intent_store=None,
                                   trim_store=None) -> list:
    from skills.execution.trade_intent_writer import TradeIntentWriter
    from skills.execution.channel_policy_guard import ChannelPolicyGuard
    from skills.execution.cooldown_guard import CooldownGuard
    from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
    from skills.execution.rth_entry_guard import RthEntryGuard
    from skills.execution.reference_price_capture import ReferencePriceCapture
    from skills.execution.sizing_resolver import SizingResolver
    from skills.execution.equity_contract_builder import EquityContractBuilder
    from skills.execution.order_sizer import OrderSizer
    from skills.execution.shares_market_submitter import SharesMarketSubmitter
    from skills.execution.options_chase_guard import OptionsChaseGuard
    from skills.execution.chain_lookup import ChainLookup
    from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
    from skills.execution.contract_selector import ContractSelector
    from skills.execution.options_market_submitter import OptionsMarketSubmitter

    intent_guards = []
    if trade_intent_store is not None:
        intent_guards = [
            TradeIntentWriter(trade_intent_store),
            ChannelPolicyGuard(policy, trade_intent_store),
            CooldownGuard(policy, trade_intent_store),
        ]

    rungs = [(i + 1, r.threshold_pct, r.trim_pct)
             for i, r in enumerate(policy.execution.trim_ladder.rungs)]

    return intent_guards + [
        ExecutionEligibilityGuard(policy),
        RthEntryGuard(),
        ReferencePriceCapture(gateway),
        SizingResolver(policy.execution),

        # Shares sub-chain
        EquityContractBuilder(gateway),
        OrderSizer(gateway, margin_multiplier=policy.execution.margin_multiplier),
        SharesMarketSubmitter(
            gateway, trade_intent_store, trim_store,
            fill_timeout=policy.execution.fill_wait_timeout_seconds,
            trim_rungs=rungs,
        ),

        # Options sub-chain (gated)
        OptionsChaseGuard(gateway,
                          threshold_pct=policy.execution.options_chase_threshold_pct),
        ChainLookup(gateway, execution_store._conn),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(gateway, margin_multiplier=policy.execution.margin_multiplier),
        OptionsMarketSubmitter(
            gateway, trade_intent_store,
            fill_timeout=policy.execution.fill_wait_timeout_seconds,
        ),
    ]
