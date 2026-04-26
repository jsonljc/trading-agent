from agent.skill import Skill


def build_phase1_chain(policy, idempotency_store, telegram_client, gateway=None) -> list:
    from skills.signal.message_normalizer import MessageNormalizer
    from skills.signal.desktop_reader import DesktopReader
    from skills.signal.signal_analyzer import SignalAnalyzer
    from skills.risk.idempotency_check import IdempotencyCheck
    from skills.posttrade.telegram_digest import TelegramDigest

    skills_list = [
        MessageNormalizer(policy),
        DesktopReader(policy),
        SignalAnalyzer(policy),
        IdempotencyCheck(policy, idempotency_store),
    ]

    if gateway is not None:
        from skills.signal.ticker_validator import TickerValidator
        skills_list.append(TickerValidator(gateway))

    skills_list.append(TelegramDigest(telegram_client, mode="signal_only"))
    return skills_list


def build_phase2b_execution_chain(policy, execution_store, gateway,
                                   trade_intent_store=None) -> list:
    from skills.execution.trade_intent_writer import TradeIntentWriter
    from skills.execution.channel_policy_guard import ChannelPolicyGuard
    from skills.execution.cooldown_guard import CooldownGuard
    from skills.execution.execution_eligibility_guard import ExecutionEligibilityGuard
    from skills.execution.chain_lookup import ChainLookup
    from skills.execution.instrument_marketability_guard import InstrumentMarketabilityGuard
    from skills.execution.contract_selector import ContractSelector
    from skills.execution.order_sizer import OrderSizer
    from skills.execution.order_pricer import OrderPricer
    from skills.execution.price_walker import PriceWalker

    guards = []
    if trade_intent_store is not None:
        guards = [
            TradeIntentWriter(trade_intent_store),
            ChannelPolicyGuard(policy, trade_intent_store),
            CooldownGuard(policy, trade_intent_store),
        ]

    return guards + [
        ExecutionEligibilityGuard(policy),
        ChainLookup(gateway, execution_store._conn),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        PriceWalker(policy, gateway, trade_intent_store),
    ]
