# Spec 2 — Execution Speed & Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut signal-to-order latency from 21–51s to ~6s by replacing three sequential LLM calls with one Haiku call (`SignalAnalyzer`), rewriting `IBGateway.get_chain()` to pre-filter before per-contract API calls, refactoring `DesktopReader` to use AX tree text as primary path, and replacing `OrderSubmitter`+`FillWaiter` with a bounded aggressive `PriceWalker`.

**Architecture:** `SignalAnalyzer` (single Haiku call) + `TickerValidator` (deterministic) replace the three Phase 1 LLM skills. `IBGateway.get_chain()` is rewritten to pre-filter to 4–6 contracts before any per-contract IB calls. `DesktopReader` validates AX tree text first; bounded screenshot is the fallback. `PriceWalker` walks the ask in configurable steps with a hard cap and structured terminal outcomes. All new execution fields land on `trade_intents` (Spec 1 schema required).

**Tech Stack:** Python 3.12+, anthropic SDK (Haiku), ib_insync, pytest-asyncio, existing `agent/` and `skills/` patterns.

**Prerequisite:** Spec 1 plan must be fully implemented first — this plan assumes `trade_intents` table, `TradeIntentStore`, and `ChannelConfig` with `auto_execute` are in place.

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `skills/signal/signal_analyzer.py` | Create | Single Haiku call: intent+ticker+side+conviction |
| `skills/signal/ticker_validator.py` | Create | Deterministic post-LLM validation |
| `skills/signal/desktop_reader.py` | Rewrite | AX primary path + bounded screenshot fallback |
| `infra/ib/gateway.py` | Modify | Rewrite `get_chain()` + add `cancel_order()` + `get_option_ask()` |
| `skills/execution/price_walker.py` | Create | Bounded walk: replaces OrderSubmitter + FillWaiter |
| `skills/execution/order_pricer.py` | Modify | Also emit `initial_reference_ask` |
| `agent/policy.py` | Modify | Add `WalkProfilesConfig`, `walk_profile` to `ExecutionPolicy` and `ChannelConfig` |
| `config/policy.yaml` | Modify | Add `walk_profiles`, default `walk_profile`, per-channel `walk_profile` |
| `agent/registry.py` | Modify | Wire `SignalAnalyzer`+`TickerValidator`; remove 3 old Phase 1 skills; wire `PriceWalker` |
| `tests/unit/test_signal_analyzer.py` | Create | Unit tests for SignalAnalyzer |
| `tests/unit/test_ticker_validator.py` | Create | Unit tests for TickerValidator |
| `tests/unit/test_desktop_reader.py` | Modify | Tests for AX validation gate + fallback |
| `tests/unit/test_price_walker.py` | Create | Unit tests for PriceWalker |
| `tests/unit/test_gateway_get_chain.py` | Create | Unit tests for new get_chain() |
| `tests/e2e/test_phase2b_execution_pipeline.py` | Modify | Assert PriceWalker fills + terminal outcomes |

---

## Task 1: SignalAnalyzer Skill

Replaces `TradeIntentDetector`, `TickerResolver`, and `ConvictionClassifier` with a single Haiku call.

**Files:**
- Create: `skills/signal/signal_analyzer.py`
- Create: `tests/unit/test_signal_analyzer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_signal_analyzer.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent.context import Context
from skills.signal.signal_analyzer import SignalAnalyzer


def _policy():
    p = MagicMock()
    p.models.text = "claude-haiku-4-5-20251001"
    return p


def _ctx(text="Initiating long NVDA calls", channel="mystic", author="trader1"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({
        "full_message_text": text,
        "channel": channel,
        "author": author,
        "received_at": "2026-04-24T10:00:00+00:00",
    })
    return ctx


def _mock_response(json_text: str):
    content = MagicMock()
    content.text = json_text
    resp = MagicMock()
    resp.content = [content]
    return resp


@pytest.mark.asyncio
async def test_valid_long_signal():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "long", '
        '"conviction": "high", "analysis_confidence": 0.95, "ambiguity_flags": [], '
        '"rationale": "Initiating long NVDA calls"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["ticker"] == "NVDA"
    assert result.updates["side"] == "long"
    assert result.updates["conviction"] == "high"
    assert result.updates["analysis_confidence"] == pytest.approx(0.95)
    assert result.updates["ambiguity_flags"] == []


@pytest.mark.asyncio
async def test_not_a_trade_signal_skips():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": false, "ticker": null, "side": "none", '
        '"conviction": "low", "analysis_confidence": 0.99, "ambiguity_flags": [], '
        '"rationale": "General market commentary"}'
    ))
    result = await skill.run(_ctx("Markets look interesting today"))
    assert result.status == "skip"


@pytest.mark.asyncio
async def test_low_confidence_ambiguous():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "long", '
        '"conviction": "medium", "analysis_confidence": 0.55, '
        '"ambiguity_flags": ["direction_unclear"], "rationale": "Unclear signal"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "ambiguous_signal" in result.reason


@pytest.mark.asyncio
async def test_ambiguity_flag_blocks_even_with_high_confidence():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "long", '
        '"conviction": "high", "analysis_confidence": 0.85, '
        '"ambiguity_flags": ["multiple_tickers_detected"], "rationale": "Two tickers"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "skip"


@pytest.mark.asyncio
async def test_parse_failure_returns_fail():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response("not json"))
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "signal_parse_failed" in result.reason


@pytest.mark.asyncio
async def test_invalid_side_enum_returns_fail():
    skill = SignalAnalyzer(_policy())
    skill._client = MagicMock()
    skill._client.messages.create = AsyncMock(return_value=_mock_response(
        '{"is_trade_signal": true, "ticker": "NVDA", "side": "buy", '
        '"conviction": "high", "analysis_confidence": 0.9, "ambiguity_flags": [],'
        ' "rationale": "test"}'
    ))
    result = await skill.run(_ctx())
    assert result.status == "fail"
    assert "signal_parse_failed" in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_signal_analyzer.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement SignalAnalyzer**

Create `skills/signal/signal_analyzer.py`:

```python
from __future__ import annotations
import json
import re
import logging
import anthropic
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)

_VALID_SIDES = {"long", "short", "none"}
_VALID_CONVICTIONS = {"high", "medium", "low"}
_VALID_FLAGS = {
    "ticker_implicit", "multiple_tickers_detected", "direction_unclear",
    "non_actionable_commentary", "slang_interpretation",
}

_SYSTEM_PROMPT = """You analyze Discord trading messages and return a JSON object. Return JSON only — no prose.

Required fields:
- is_trade_signal: boolean — true only if the author is entering/adding to a position
- ticker: string or null — the stock ticker symbol
- side: "long" | "short" | "none"
- conviction: "high" | "medium" | "low"
- analysis_confidence: float 0.0–1.0 — your confidence in the extraction
- ambiguity_flags: array — zero or more of: ticker_implicit, multiple_tickers_detected, direction_unclear, non_actionable_commentary, slang_interpretation
- rationale: string — one sentence explaining the classification

Set is_trade_signal=false for commentary, news, watchlist mentions, and analysis without position entry."""


def _safe_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


class SignalAnalyzer(Skill):
    name = "SignalAnalyzer"

    def __init__(self, policy) -> None:
        self._policy = policy
        self._client = anthropic.AsyncAnthropic()

    async def run(self, ctx: Context) -> SkillResult:
        text = ctx.get("full_message_text", "")
        channel = ctx.get("channel", "")
        author = ctx.get("author", "")

        response = await self._client.messages.create(
            model=self._policy.models.text,
            max_tokens=256,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"Channel: #{channel}\nAuthor: {author}\nMessage: {text}"}],
        )

        parsed = _safe_json(response.content[0].text)
        if not parsed or "is_trade_signal" not in parsed:
            return SkillResult(status="fail", reason="signal_parse_failed: could not parse SignalAnalyzer response")

        # Validate enum fields
        side = parsed.get("side", "none")
        conviction = parsed.get("conviction", "low")
        flags = parsed.get("ambiguity_flags", [])

        if side not in _VALID_SIDES:
            return SkillResult(status="fail", reason=f"signal_parse_failed: invalid side '{side}'")
        if conviction not in _VALID_CONVICTIONS:
            return SkillResult(status="fail", reason=f"signal_parse_failed: invalid conviction '{conviction}'")
        for flag in flags:
            if flag not in _VALID_FLAGS:
                return SkillResult(status="fail", reason=f"signal_parse_failed: unknown flag '{flag}'")

        if not parsed.get("is_trade_signal"):
            return SkillResult(status="skip", reason=f"not_a_trade_signal: {parsed.get('rationale', '')}")

        confidence = float(parsed.get("analysis_confidence", 0.0))
        if confidence < 0.70 or flags:
            return SkillResult(
                status="skip",
                reason=f"ambiguous_signal: confidence={confidence:.2f} flags={flags}",
            )

        ticker = (parsed.get("ticker") or "").upper().strip()
        if not ticker:
            return SkillResult(status="fail", reason="signal_parse_failed: ticker is null")

        return SkillResult(status="success", updates={
            "ticker": ticker,
            "ticker_raw": parsed.get("ticker"),
            "side": side,
            "side_raw": side,
            "conviction": conviction,
            "conviction_raw": conviction,
            "analysis_confidence": confidence,
            "ambiguity_flags": json.dumps(flags),
            "rationale": parsed.get("rationale", ""),
            # Keep backward-compat keys used by legacy Phase 2b skills
            "intent": "LONG_SIGNAL" if side == "long" else "SHORT_SIGNAL",
            "conviction_bucket": conviction,
        })
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_signal_analyzer.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/signal/signal_analyzer.py tests/unit/test_signal_analyzer.py
git commit -m "feat(signal): add SignalAnalyzer — single Haiku call replaces 3 LLM skills"
```

---

## Task 2: TickerValidator Skill

**Files:**
- Create: `skills/signal/ticker_validator.py`
- Create: `tests/unit/test_ticker_validator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_ticker_validator.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.signal.ticker_validator import TickerValidator


def _ctx(ticker="NVDA", side="long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"ticker": ticker, "side": side})
    return ctx


def _gateway(qualifies=True):
    gw = MagicMock()
    if qualifies:
        ref = MagicMock()
        ref.qualified = True
        gw.qualify = AsyncMock(return_value=ref)
    else:
        from infra.ib.gateway import IBGatewayUnavailable
        gw.qualify = AsyncMock(side_effect=IBGatewayUnavailable("not found"))
    return gw


@pytest.mark.asyncio
async def test_valid_ticker_passes():
    skill = TickerValidator(_gateway(qualifies=True))
    result = await skill.run(_ctx())
    assert result.status == "success"


@pytest.mark.asyncio
async def test_unresolvable_ticker_fails():
    skill = TickerValidator(_gateway(qualifies=False))
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "ambiguous_signal" in result.reason


@pytest.mark.asyncio
async def test_missing_ticker_fails():
    skill = TickerValidator(_gateway())
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({"side": "long"})
    result = await skill.run(ctx)
    assert result.status == "fail"


@pytest.mark.asyncio
async def test_ambiguous_side_fails():
    skill = TickerValidator(_gateway())
    result = await skill.run(_ctx(side="none"))
    assert result.status == "skip"
    assert "ambiguous_signal" in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_ticker_validator.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement TickerValidator**

Create `skills/signal/ticker_validator.py`:

```python
from __future__ import annotations
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import BrokerContractRef

logger = logging.getLogger(__name__)


class TickerValidator(Skill):
    name = "TickerValidator"

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def run(self, ctx: Context) -> SkillResult:
        ticker = ctx.get("ticker")
        side = ctx.get("side", "")

        if not ticker:
            return SkillResult(status="fail", reason="ticker_validator: ticker missing from context")

        if side not in ("long", "short"):
            return SkillResult(
                status="skip",
                reason=f"ambiguous_signal: side='{side}' is not long or short",
            )

        ref = BrokerContractRef(
            symbol=ticker, sec_type="STK", exchange="SMART", currency="USD"
        )
        try:
            qualified = await self._gateway.qualify(ref)
            if not qualified.qualified:
                raise IBGatewayUnavailable(f"qualify returned unqualified ref for {ticker}")
        except IBGatewayUnavailable as exc:
            return SkillResult(
                status="skip",
                reason=f"ambiguous_signal: ticker '{ticker}' could not be validated: {exc}",
            )

        logger.info("TickerValidator: %s validated (side=%s)", ticker, side)
        return SkillResult(status="success")
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_ticker_validator.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/signal/ticker_validator.py tests/unit/test_ticker_validator.py
git commit -m "feat(signal): add TickerValidator for deterministic post-LLM validation"
```

---

## Task 3: IBGateway — Rewrite get_chain() + Add cancel_order() + get_option_ask()

**Files:**
- Modify: `infra/ib/gateway.py`
- Create: `tests/unit/test_gateway_get_chain.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_gateway_get_chain.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from infra.ib.gateway import IBGateway
from infra.ib.models import BrokerContractRef


def _policy(min_expiry_days=180):
    p = MagicMock()
    p.ib_gateway.host = "127.0.0.1"
    p.ib_gateway.port = 4002
    p.ib_gateway.client_id = 1
    p.ib_gateway.mode = "paper"
    p.ib_gateway.paper_account_prefixes = ["DU"]
    p.instrument_policy.min_expiry_days = min_expiry_days
    return p


def _make_chain(strikes, expirations):
    chain = MagicMock()
    chain.strikes = strikes
    chain.expirations = expirations
    return chain


@pytest.mark.asyncio
async def test_get_chain_pre_filters_to_calls_near_spot():
    """get_chain() should only qualify calls within the strike window."""
    from datetime import date, timedelta
    today = date.today()
    near_expiry = (today + timedelta(days=30)).strftime("%Y%m%d")   # filtered out (< min_expiry_days)
    far_expiry  = (today + timedelta(days=200)).strftime("%Y%m%d")  # kept

    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()
    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=lambda *a: [a[0]])

    stock_ref = MagicMock()
    stock_ref.conId = 12345
    gw._ib.qualifyContractsAsync = AsyncMock(return_value=[stock_ref])

    chain = _make_chain(
        strikes=[140.0, 145.0, 148.0, 150.0, 152.0, 155.0, 160.0, 165.0],
        expirations=[near_expiry, far_expiry],
    )
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    ticker_mock = MagicMock()
    ticker_mock.bid = 2.0
    ticker_mock.ask = 2.5
    ticker_mock.bid.__eq__ = lambda s, o: s == o
    gw._ib.reqTickersAsync = AsyncMock(return_value=[ticker_mock])

    # Qualify calls will be called for pre-filtered candidates only
    qualify_calls = []
    async def fake_qualify(contract):
        qualify_calls.append(contract)
        c = MagicMock()
        c.symbol = "NVDA"
        c.secType = "OPT"
        c.exchange = "SMART"
        c.currency = "USD"
        c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike
        c.right = contract.right
        c.multiplier = "100"
        c.localSymbol = None
        c.tradingClass = None
        return [c]

    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=fake_qualify)

    # spot_price = 152.0 → ITM calls: 148, 150 (≤ 152); ATM/OTM: 155
    # near_expiry filtered → only far_expiry kept
    # Only "C" (calls) kept
    spot = 152.0
    candidates = await gw.get_chain("NVDA", spot_price=spot)

    # All returned candidates must be calls and from far_expiry
    for c in candidates:
        assert c.right == "C"
        assert c.expiry == f"{far_expiry[:4]}-{far_expiry[4:6]}-{far_expiry[6:]}"
    # Strike window: ITM (≤ spot) last 3: 148, 150, 152 + OTM next 2: 155, 160
    strikes = {c.strike for c in candidates}
    assert 148.0 in strikes or 150.0 in strikes  # at least one ITM
    assert 165.0 not in strikes  # far OTM excluded


@pytest.mark.asyncio
async def test_get_chain_partial_qualify_failures_skipped():
    """Contracts that fail qualification are dropped, not fatal."""
    from datetime import date, timedelta
    far_expiry = (date.today() + timedelta(days=200)).strftime("%Y%m%d")

    gw = IBGateway(_policy(min_expiry_days=180))
    gw._ib = MagicMock()

    stock_ref = MagicMock()
    stock_ref.conId = 12345
    gw._ib.qualifyContractsAsync = AsyncMock(return_value=[stock_ref])

    chain = _make_chain(strikes=[150.0, 155.0], expirations=[far_expiry])
    gw._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    call_count = [0]
    async def flaky_qualify(contract):
        call_count[0] += 1
        if call_count[0] == 1:
            return []  # first call fails
        c = MagicMock()
        c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.conId = 99
        c.lastTradeDateOrContractMonth = far_expiry
        c.strike = contract.strike; c.right = contract.right
        c.multiplier = "100"; c.localSymbol = None; c.tradingClass = None
        return [c]

    gw._ib.qualifyContractsAsync = AsyncMock(side_effect=flaky_qualify)
    td = MagicMock(); td.bid = 2.0; td.ask = 2.5
    gw._ib.reqTickersAsync = AsyncMock(return_value=[td])

    candidates = await gw.get_chain("NVDA", spot_price=152.0)
    assert len(candidates) >= 1  # at least one survived
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_gateway_get_chain.py -v
```

Expected: FAIL — `get_chain()` does not accept `spot_price` parameter yet.

- [ ] **Step 3: Rewrite get_chain() in gateway.py**

Replace the existing `get_chain` method in `infra/ib/gateway.py`:

```python
    async def get_chain(self, ticker: str, spot_price: float | None = None) -> list[OptionCandidate]:
        self._read_breaker.check()
        try:
            from ib_insync import Stock, Option
            from datetime import date, timedelta

            # Step 1+2 in parallel: chain params + spot price
            stock = Stock(ticker, "SMART", "USD")
            if spot_price is not None:
                qualified_stocks = await self._ib.qualifyContractsAsync(stock)
                spot = spot_price
            else:
                qualified_stocks, spot = await asyncio.gather(
                    self._ib.qualifyContractsAsync(stock),
                    self._fetch_spot(ticker),
                )
            if not qualified_stocks:
                self._read_breaker._record_success()
                return []
            underlying_con_id = qualified_stocks[0].conId

            chains = await self._ib.reqSecDefOptParamsAsync(ticker, "", "STK", underlying_con_id)
            if not chains:
                self._read_breaker._record_success()
                return []
            chain = chains[0]

            # Step 3: in-process pre-filter (zero IBKR calls)
            min_expiry = self._policy.instrument_policy.min_expiry_days
            cutoff = date.today() + timedelta(days=min_expiry)
            valid_expiries = [
                e for e in chain.expirations
                if date(int(e[:4]), int(e[4:6]), int(e[6:])) >= cutoff
            ]

            # Strike window: 3 at/below spot (ITM) + 2 above (ATM/OTM)
            all_strikes = sorted(chain.strikes)
            itm = [s for s in all_strikes if s <= spot][-3:]
            otm = [s for s in all_strikes if s > spot][:2]
            selected_strikes = set(itm + otm)

            pre_filtered = [
                (expiry, strike, "C")
                for expiry in valid_expiries
                for strike in selected_strikes
            ]

            if not pre_filtered:
                self._read_breaker._record_success()
                return []

            # Step 4: qualify + quote the 4–6 surviving contracts in parallel
            async def _qualify_and_quote(expiry: str, strike: float, right: str):
                opt = Option(ticker, expiry, strike, right, "SMART")
                try:
                    qualified = await self._ib.qualifyContractsAsync(opt)
                    if not qualified:
                        return None
                    q = qualified[0]
                    tickers = await self._ib.reqTickersAsync(q)
                    if not tickers:
                        return None
                    td = tickers[0]
                    bid = float(td.bid) if td.bid and td.bid == td.bid and td.bid > 0 else 0.0
                    ask = float(td.ask) if td.ask and td.ask == td.ask and td.ask > 0 else 0.0
                    mid = (bid + ask) / 2
                    spread_pct = ((ask - bid) / ask) if ask > 0 else 1.0
                    ref = _from_ib_contract(q)
                    ref.qualified = True
                    expiry_fmt = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
                    return OptionCandidate(
                        symbol=ticker, expiry=expiry_fmt, strike=strike, right=right,
                        bid=bid, ask=ask, mid=mid, spread_pct=spread_pct,
                        open_interest=None, volume=None,
                        multiplier=int(q.multiplier or 100), contract_ref=ref,
                    )
                except Exception:
                    return None

            results = await _asyncio.gather(*[
                _qualify_and_quote(e, s, r) for e, s, r in pre_filtered
            ])
            candidates = [c for c in results if c is not None]

            if len(candidates) < 2:
                self._read_breaker._record_failure()
                raise IBGatewayUnavailable("chain_lookup_insufficient_candidates")

            self._read_breaker._record_success()
            return candidates
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_chain failed: {exc}") from exc

    async def _fetch_spot(self, ticker: str) -> float:
        return await self.get_quote(ticker)
```

Also add `cancel_order` and `get_option_ask` methods to `IBGateway`:

```python
    async def cancel_order(self, trade, timeout: float = 5.0) -> bool:
        """Cancel an order and wait for terminal broker state. Returns True if confirmed cancelled."""
        import time as _time
        self._ib.cancelOrder(trade.order)
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            status = trade.orderStatus.status
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                return True
        logger.warning("cancel_order: timed out waiting for cancel confirmation")
        return False

    async def get_option_ask(self, contract_ref: BrokerContractRef) -> tuple[float, float]:
        """Returns (ask, age_seconds) for a qualified option contract.

        Uses reqTickersAsync snapshot — a fast single-contract call.
        Returns (0.0, float('inf')) on failure.
        """
        self._read_breaker.check()
        import time as _time
        try:
            ib_contract = _to_ib_contract(contract_ref)
            tickers = await self._ib.reqTickersAsync(ib_contract)
            if not tickers:
                return 0.0, float("inf")
            td = tickers[0]
            ask = float(td.ask) if td.ask and td.ask == td.ask and td.ask > 0 else 0.0
            # reqTickersAsync returns a snapshot; age is effectively 0
            self._read_breaker._record_success()
            return ask, 0.0
        except IBGatewayUnavailable:
            raise
        except Exception as exc:
            self._read_breaker._record_failure()
            raise IBGatewayUnavailable(f"get_option_ask failed: {exc}") from exc
```

- [ ] **Step 4: Run gateway tests**

```
pytest tests/unit/test_gateway_get_chain.py tests/unit/test_gateway_circuit_breaker.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add infra/ib/gateway.py tests/unit/test_gateway_get_chain.py
git commit -m "feat(gateway): rewrite get_chain() with pre-filter+parallel; add cancel_order, get_option_ask"
```

---

## Task 4: DesktopReader Refactor

Replaces the current screenshot-only approach with AX primary path + bounded fallback.

**Files:**
- Rewrite: `skills/signal/desktop_reader.py`
- Modify: `tests/unit/test_desktop_reader.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_desktop_reader.py` (or replace existing tests):

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent.context import Context
from skills.signal.desktop_reader import DesktopReader


def _policy():
    p = MagicMock()
    p.models.text = "claude-haiku-4-5-20251001"
    p.models.vision = "claude-opus-4-7"
    return p


def _ctx(preview: str = "", source: str = "reconciliation"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({
        "full_message_text": preview,
        "trigger_preview": preview,
        "channel": "mystic",
        "capture_mode": source,
    })
    return ctx


@pytest.mark.asyncio
async def test_valid_ax_text_skips_screenshot():
    """Long clean AX text passes validation gate — no screenshot taken."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock()
    ctx = _ctx("Initiating a long position in NVDA calls high conviction entry here today")
    result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates.get("capture_mode") == "ax"
    skill._bounded_screenshot_extract.assert_not_called()


@pytest.mark.asyncio
async def test_nav_pattern_triggers_screenshot_fallback():
    """Text matching nav chrome triggers bounded screenshot fallback."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock(return_value=(
        "Initiating long NVDA calls here with strong conviction", "screenshot"
    ))
    ctx = _ctx("Stock Talk Insiders 丨 mystic")
    result = await skill.run(ctx)
    skill._bounded_screenshot_extract.assert_called_once()
    assert result.updates.get("capture_mode") in ("screenshot", "preview_fallback")


@pytest.mark.asyncio
async def test_short_text_triggers_screenshot_fallback():
    """Text shorter than 40 chars triggers fallback."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock(return_value=(
        "Long NVDA calls initiating position today with strong conviction", "screenshot"
    ))
    ctx = _ctx("short text")
    result = await skill.run(ctx)
    skill._bounded_screenshot_extract.assert_called_once()


@pytest.mark.asyncio
async def test_screenshot_timeout_uses_preview_fallback():
    """If screenshot extraction raises, use preview text as fallback."""
    skill = DesktopReader(_policy())
    skill._bounded_screenshot_extract = AsyncMock(side_effect=Exception("timeout"))
    ctx = _ctx("Short preview text")
    result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates.get("capture_mode") == "preview_fallback"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_desktop_reader.py -v
```

Expected: FAIL — tests reference `_bounded_screenshot_extract` which doesn't exist yet.

- [ ] **Step 3: Rewrite desktop_reader.py**

Replace `skills/signal/desktop_reader.py` entirely:

```python
from __future__ import annotations
import asyncio
import base64
import logging
import subprocess
import tempfile
import time
from pathlib import Path

import anthropic

from agent.context import Context, SkillResult
from agent.skill import Skill
from agent.policy import PolicyModel

logger = logging.getLogger(__name__)

_AX_MIN_LENGTH = 40
_SCREENSHOT_TIMEOUT_S = 1.0
_NAV_PATTERNS = ("Stock Talk Insiders", "丨", "#")


def _passes_ax_validation(text: str) -> bool:
    if len(text.strip()) < _AX_MIN_LENGTH:
        return False
    for pattern in _NAV_PATTERNS:
        if pattern in text:
            return False
    return True


class DesktopReader(Skill):
    name = "desktop_reader"

    def __init__(self, policy: PolicyModel) -> None:
        self._policy = policy
        self._client = anthropic.AsyncAnthropic()

    async def run(self, ctx: Context) -> SkillResult:
        preview = ctx.get("full_message_text", ctx.get("trigger_preview", ""))

        # Option A: AX primary path
        if _passes_ax_validation(preview):
            return SkillResult(status="success", updates={
                "full_message_text": preview,
                "capture_mode": "ax",
            })

        # Option B: bounded screenshot fallback
        try:
            text, mode = await self._bounded_screenshot_extract(preview)
            return SkillResult(status="success", updates={
                "full_message_text": text,
                "capture_mode": mode,
            })
        except Exception as exc:
            logger.warning("DesktopReader: screenshot fallback failed (%s) — using preview", exc)
            return SkillResult(status="success", updates={
                "full_message_text": preview,
                "capture_mode": "preview_fallback",
            })

    async def _bounded_screenshot_extract(self, fallback_text: str) -> tuple[str, str]:
        """1-second SLA screenshot + Haiku extraction. Raises on timeout."""
        loop = asyncio.get_event_loop()
        try:
            image_data = await asyncio.wait_for(
                loop.run_in_executor(None, self._capture_message_pane),
                timeout=_SCREENSHOT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise Exception("screenshot_timeout")

        b64 = base64.standard_b64encode(image_data).decode()
        response = await asyncio.wait_for(
            self._client.messages.create(
                model=self._policy.models.text,
                max_tokens=512,
                system="Extract the Discord message text verbatim. Return only the message text.",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                        {"type": "text", "text": "Extract all Discord message text you can see."},
                    ],
                }],
            ),
            timeout=_SCREENSHOT_TIMEOUT_S,
        )
        return response.content[0].text.strip(), "screenshot"

    def _capture_message_pane(self) -> bytes:
        """Capture Discord message pane region only (no channel navigation)."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            subprocess.run(["screencapture", "-x", path], check=True, capture_output=True)
            return Path(path).read_bytes()
        finally:
            import os
            try:
                os.unlink(path)
            except OSError:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_desktop_reader.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/signal/desktop_reader.py tests/unit/test_desktop_reader.py
git commit -m "feat(signal): refactor DesktopReader — AX primary path, bounded screenshot fallback"
```

---

## Task 5: OrderPricer — emit initial_reference_ask

`PriceWalker` needs `initial_reference_ask` to compute the max chase cap.

**Files:**
- Modify: `skills/execution/order_pricer.py`
- Modify: `tests/unit/test_order_pricer.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_order_pricer.py`:

```python
@pytest.mark.asyncio
async def test_order_pricer_emits_initial_reference_ask(db):
    # (Reuse whatever _ctx() and _candidate() helpers exist in that test file)
    # If they don't exist, create minimal versions:
    from agent.context import Context
    from skills.execution.order_pricer import OrderPricer
    from infra.ib.models import OptionCandidate, BrokerContractRef
    from datetime import date, timedelta
    from unittest.mock import MagicMock

    policy = MagicMock()
    policy.pricing_policy_guards.min_bid = 0.01
    policy.pricing_policy_guards.max_spread_pct = 0.40
    policy.pricing_policy.option_spread_fraction = 0.25
    policy.execution.max_equity_price = 500.0

    expiry = (date.today() + timedelta(days=200)).strftime("%Y-%m-%d")
    ref = BrokerContractRef(symbol="NVDA", sec_type="OPT", exchange="SMART",
                             currency="USD", qualified=True)
    candidate = OptionCandidate(
        symbol="NVDA", expiry=expiry, strike=150.0, right="C",
        bid=5.0, ask=5.50, mid=5.25, spread_pct=0.09,
        open_interest=100, volume=50, multiplier=100, contract_ref=ref,
    )

    ctx = Context(trace_id="t1", event_id="e1")
    ctx.update({"instrument_type": "option", "option_candidates": [candidate],
                "selected_strike": 150.0})

    skill = OrderPricer(policy)
    result = await skill.run(ctx)
    assert result.status == "success"
    assert result.updates["initial_reference_ask"] == pytest.approx(5.50)
```

- [ ] **Step 2: Run failing test**

```
pytest tests/unit/test_order_pricer.py::test_order_pricer_emits_initial_reference_ask -v
```

Expected: FAIL — `initial_reference_ask` not in updates.

- [ ] **Step 3: Update OrderPricer to emit initial_reference_ask**

In `skills/execution/order_pricer.py`, in the `instrument_type == "option"` branch, after computing `limit_price`, add `"initial_reference_ask": c.ask` to the return updates:

```python
        logger.info("OrderPricer: limit_price=%.2f type=%s", limit_price, instrument_type)
        updates = {"limit_price": limit_price, "order_type": "LMT"}
        if instrument_type == "option":
            updates["initial_reference_ask"] = c.ask
        return SkillResult(status="success", updates=updates)
```

Also replace the final `return` statement to use `updates`.

- [ ] **Step 4: Run tests**

```
pytest tests/unit/test_order_pricer.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/order_pricer.py tests/unit/test_order_pricer.py
git commit -m "feat(execution): OrderPricer emits initial_reference_ask for PriceWalker cap"
```

---

## Task 6: PriceWalker Skill

Replaces `OrderSubmitter` + `FillWaiter` with a bounded walk.

**Files:**
- Create: `skills/execution/price_walker.py`
- Create: `tests/unit/test_price_walker.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_price_walker.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent.context import Context
from skills.execution.price_walker import PriceWalker
from infra.ib.models import BrokerContractRef, FillStatus


def _policy(walk_profile="aggressive_fast", max_chase_pct=0.15, reprice_interval_ms=2500):
    p = MagicMock()
    p.execution.walk_profile = walk_profile
    p.execution.walk_profiles = {
        "aggressive_fast": [0.01, 0.03, 0.06, 0.10],
        "cautious_fast":   [0.00, 0.02, 0.05, 0.10],
    }
    p.execution.max_chase_pct = max_chase_pct
    p.execution.reprice_interval_ms = reprice_interval_ms
    p.ib_gateway.mode = "paper"
    p.ib_gateway.port = 4002
    p.ib_gateway.paper_account_prefixes = ["DU"]
    return p


def _contract():
    return BrokerContractRef(
        symbol="NVDA", sec_type="OPT", exchange="SMART", currency="USD",
        expiry="20261218", strike=150.0, right="C", qualified=True
    )


def _gateway(ask=5.50, fill=True):
    fake_trade = MagicMock()
    fake_trade.order.orderId = "IB-1"
    fake_trade.order.permId = 42
    fill_status = MagicMock()
    fill_status.status = "Filled" if fill else "Submitted"
    fill_status.filled = 9
    fill_status.remaining = 0
    fill_status.avgFillPrice = 5.56
    fake_trade.orderStatus = fill_status

    gw = MagicMock()
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.cancel_order = AsyncMock(return_value=True)
    gw.get_option_ask = AsyncMock(return_value=(ask, 0.0))
    gw._account_id = "DU12345"
    return gw, fake_trade


def _store():
    s = MagicMock()
    s.update_execution_state = AsyncMock()
    s.update_outbox_status = AsyncMock()
    return s


def _ctx(ticker="NVDA", quantity=9, limit_price=5.56, initial_reference_ask=5.50,
         execution_mode="auto_live", intent_id="evt1:NVDA:long"):
    ctx = Context(trace_id="t1", event_id="evt1")
    ctx.update({
        "ticker": ticker,
        "quantity": quantity,
        "limit_price": limit_price,
        "initial_reference_ask": initial_reference_ask,
        "selected_contract": _contract(),
        "execution_mode": execution_mode,
        "intent_id": intent_id,
        "signal_id": "sig1",
        "action": "BUY",
    })
    return ctx


@pytest.mark.asyncio
async def test_fills_on_first_step(db_or_none=None):
    """Happy path: first order fills immediately."""
    from infra.storage.trade_intent_store import TradeIntentStore
    import aiosqlite

    gw, trade = _gateway(ask=5.50, fill=True)
    trade.orderStatus.status = "Filled"

    store = _store()
    skill = PriceWalker(_policy(), gw, store)
    result = await skill.run(_ctx())
    assert result.status == "success"
    assert result.updates["fill_status"] == "filled"
    assert result.updates["order_attempt_count"] == 1
    gw.place_order.assert_called_once()
    gw.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_walk_exhausted_after_all_steps():
    """All steps run without fill → cancelled_unfilled / walk_exhausted."""
    gw, trade = _gateway(ask=5.50, fill=False)
    trade.orderStatus.status = "Submitted"

    gw.cancel_order = AsyncMock(side_effect=lambda t, **kw: setattr(
        t.orderStatus, 'status', 'Cancelled') or True
    )

    store = _store()
    skill = PriceWalker(_policy(reprice_interval_ms=50), gw, store)
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "walk_exhausted" in result.reason or "cancelled_unfilled" in result.reason


@pytest.mark.asyncio
async def test_price_exceeded_cap_stops_walk_early():
    """If the next step price would exceed max_chase_price, stop before placing."""
    # initial_reference_ask=5.00, max_chase_pct=0.05 → max_chase_price=5.25
    # step buffers: [0.01, 0.03, 0.06, 0.10]
    # With ask=5.50: step3 = 5.50 * 1.06 = 5.83 > 5.25 → stop
    gw, trade = _gateway(ask=5.50, fill=False)
    trade.orderStatus.status = "Submitted"
    gw.cancel_order = AsyncMock(side_effect=lambda t, **kw: setattr(
        t.orderStatus, 'status', 'Cancelled') or True
    )

    store = _store()
    skill = PriceWalker(_policy(max_chase_pct=0.05, reprice_interval_ms=50), gw, store)
    ctx = _ctx(initial_reference_ask=5.00)
    result = await skill.run(ctx)
    assert result.status == "skip"
    assert "price_exceeded_cap" in result.reason or "cancelled_unfilled" in result.reason


@pytest.mark.asyncio
async def test_stale_quote_terminates_walk():
    """Quote age > 5s terminates walk immediately."""
    gw, trade = _gateway(ask=5.50, fill=False)
    gw.get_option_ask = AsyncMock(return_value=(5.50, 6.0))  # 6 seconds old → stale

    store = _store()
    skill = PriceWalker(_policy(reprice_interval_ms=50), gw, store)
    result = await skill.run(_ctx())
    assert result.status == "skip"
    assert "stale_quote" in result.reason
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/test_price_walker.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement PriceWalker**

Create `skills/execution/price_walker.py`:

```python
from __future__ import annotations
import asyncio
import logging
import math
import uuid
from datetime import datetime, timezone
from agent.context import Context, SkillResult
from agent.skill import Skill
from infra.ib.gateway import IBGatewayUnavailable
from infra.ib.models import PreparedOrder

logger = logging.getLogger(__name__)

_STALE_QUOTE_THRESHOLD_S = 5.0
_CANCEL_WAIT_TIMEOUT_S = 5.0


def _round_up_to_tick(price: float, tick: float = 0.05) -> float:
    return math.ceil(price / tick) * tick


class PriceWalker(Skill):
    name = "PriceWalker"

    def __init__(self, policy, gateway, trade_intent_store) -> None:
        self._policy = policy
        self._gateway = gateway
        self._store = trade_intent_store

    async def run(self, ctx: Context) -> SkillResult:
        ep = self._policy.execution
        intent_id = ctx.get("intent_id")
        contract_ref = ctx.get("selected_contract")
        quantity = ctx.get("quantity")
        initial_reference_ask = ctx.get("initial_reference_ask")

        if not all([contract_ref, quantity, initial_reference_ask]):
            return SkillResult(status="fail",
                               reason="price_walker: missing contract_ref, quantity, or initial_reference_ask")

        # Walk profile: prefer per-context override, then policy default
        profile_name = ctx.get("walk_profile") or ep.walk_profile
        step_buffers: list[float] = ep.walk_profiles.get(profile_name, ep.walk_profiles["aggressive_fast"])
        max_chase_price = initial_reference_ask * (1.0 + ep.max_chase_pct)
        reprice_interval_s = ep.reprice_interval_ms / 1000.0

        order_submitted_at = None
        attempt_count = 0
        trade = None
        last_limit_price = None

        for step_idx, step_buffer in enumerate(step_buffers):
            # Step 1: get live ask + check staleness
            try:
                ask, age_s = await self._gateway.get_option_ask(contract_ref)
            except IBGatewayUnavailable as exc:
                await self._mark_failed(intent_id, f"broker_unavailable: {exc}")
                return SkillResult(status="fail", reason=f"price_walker broker error: {exc}")

            if age_s > _STALE_QUOTE_THRESHOLD_S:
                reason = "stale_quote"
                await self._mark_cancelled(intent_id, reason)
                return SkillResult(status="skip", reason=f"cancelled_unfilled: {reason}",
                                   updates=self._terminal_updates(attempt_count, last_limit_price))

            # Step 2: compute limit price
            raw_limit = ask * (1.0 + step_buffer)
            if raw_limit > max_chase_price:
                reason = "price_exceeded_cap"
                await self._mark_cancelled(intent_id, reason)
                return SkillResult(status="skip", reason=f"cancelled_unfilled: {reason}",
                                   updates=self._terminal_updates(attempt_count, last_limit_price))

            # Step 3: round up to tick
            limit_price = _round_up_to_tick(min(raw_limit, max_chase_price))
            last_limit_price = limit_price

            # Step 4: place order
            order = PreparedOrder(
                action=ctx.get("action", "BUY"),
                quantity=quantity,
                order_type="LMT",
                limit_price=limit_price,
                tif="DAY",
            )
            idempotency_key = f"{ctx.trace_id}:PriceWalker:{ctx.event_id}:step{step_idx}"
            submitted_at = datetime.now(timezone.utc).isoformat()

            # Set outbox_status=pending BEFORE first broker call (transactional outbox)
            if order_submitted_at is None and intent_id:
                await self._store.update_outbox_status(intent_id, "pending")

            try:
                trade = await self._gateway.place_order(contract_ref, order, idempotency_key)
            except IBGatewayUnavailable as exc:
                await self._mark_failed(intent_id, f"broker_unavailable: {exc}")
                return SkillResult(status="fail", reason=f"price_walker broker error: {exc}")

            ack_at = datetime.now(timezone.utc).isoformat()
            attempt_count += 1
            if order_submitted_at is None:
                order_submitted_at = submitted_at
                now = ack_at
                if intent_id:
                    await self._store.update_execution_state(
                        intent_id,
                        execution_state="submitted",
                        outbox_status="dispatched",
                        order_submitted_at=order_submitted_at,
                        order_ack_at=ack_at,
                        initial_order_limit=limit_price,
                        broker_order_ref=str(trade.order.orderId),
                        walk_profile=profile_name,
                        max_chase_pct=ep.max_chase_pct,
                        max_chase_price=max_chase_price,
                        initial_reference_ask=initial_reference_ask,
                    )

            # Step 5: wait for fill within reprice_interval
            deadline = asyncio.get_event_loop().time() + reprice_interval_s
            filled = False
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.5)
                status = trade.orderStatus.status
                if status == "Filled":
                    filled = True
                    break
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    break

            if filled:
                fill_price = float(trade.orderStatus.avgFillPrice or limit_price)
                filled_at = datetime.now(timezone.utc).isoformat()
                if intent_id:
                    await self._store.update_execution_state(
                        intent_id,
                        execution_state="filled",
                        outbox_status="confirmed",
                        fill_price=fill_price,
                        filled_at=filled_at,
                        order_attempt_count=attempt_count,
                        last_limit_price=limit_price,
                    )
                logger.info("PriceWalker: filled %s qty=%d @ %.2f (step %d)",
                            ctx.get("ticker"), quantity, fill_price, step_idx)
                return SkillResult(status="success", updates={
                    "fill_status": "filled",
                    "fill_price": fill_price,
                    "filled_qty": int(trade.orderStatus.filled),
                    "avg_fill_price": fill_price,
                    "order_attempt_count": attempt_count,
                    "last_limit_price": limit_price,
                })

            # Not filled: cancel and advance to next step
            if step_idx < len(step_buffers) - 1:
                await self._gateway.cancel_order(trade, timeout=_CANCEL_WAIT_TIMEOUT_S)

        # All steps exhausted
        reason = "walk_exhausted"
        await self._mark_cancelled(intent_id, reason)
        return SkillResult(status="skip", reason=f"cancelled_unfilled: {reason}",
                           updates=self._terminal_updates(attempt_count, last_limit_price))

    async def _mark_cancelled(self, intent_id: str | None, cancel_reason: str) -> None:
        if intent_id:
            await self._store.update_execution_state(
                intent_id,
                execution_state="cancelled_unfilled",
                cancel_reason=cancel_reason,
                cancelled_at=datetime.now(timezone.utc).isoformat(),
            )

    async def _mark_failed(self, intent_id: str | None, dlq_reason: str) -> None:
        if intent_id:
            await self._store.update_execution_state(
                intent_id,
                execution_state="failed",
                dlq_reason=dlq_reason,
            )

    def _terminal_updates(self, attempt_count: int, last_limit_price: float | None) -> dict:
        return {
            "fill_status": "cancelled_unfilled",
            "order_attempt_count": attempt_count,
            "last_limit_price": last_limit_price,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/test_price_walker.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/execution/price_walker.py tests/unit/test_price_walker.py
git commit -m "feat(execution): add PriceWalker — bounded aggressive walk replaces OrderSubmitter+FillWaiter"
```

---

## Task 7: Policy Model — Walk Profile Config

**Files:**
- Modify: `agent/policy.py`
- Modify: `config/policy.yaml`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_policy.py`:

```python
def test_walk_profiles_loaded():
    raw = """
trigger:
  action_words: ["long"]
instrument_policy:
  prefer_options: true
  min_expiry_days: 180
  strike_policy: closest_itm_call
  fallback_to_stock_if_no_options: true
pricing_policy:
  mode: cheapest_fillable_limit
  option_spread_fraction: 0.25
  stock_buffer_pct: 0.001
sizing_policy:
  low_conviction_pct: 0.05
  high_conviction_pct: 0.10
market_hours:
  options_rth_only: true
  stock_premarket_allowed: true
  stock_premarket_start: "04:00"
  rth_start: "09:30"
  rth_end: "16:00"
  stock_afterhours_queue: true
cooldown_policy:
  enabled: true
  cooldown_minutes: 30
dedupe_policy:
  enabled: true
  key: message_fingerprint_plus_ticker_plus_action_plus_window
pricing_policy_guards:
  min_bid: 0.01
  max_spread_pct: 0.40
models:
  vision: claude-opus-4-7
  text: claude-haiku-4-5-20251001
watched_channels:
  mystic:
    auto_execute: true
discord_bundle_id: "com.hnc.Discord"
telegram:
  chat_id: "123"
  bot_token: "fake"
execution:
  fill_wait_timeout_seconds: 30
  max_equity_price: 500.0
  reconciler_interval_seconds: 60
  walk_profile: aggressive_fast
  walk_profiles:
    cautious_fast:   [0.00, 0.02, 0.05, 0.10]
    aggressive_fast: [0.01, 0.03, 0.06, 0.10]
  reprice_interval_ms: 2500
  max_chase_pct: 0.15
"""
    import yaml
    from agent.policy import PolicyModel
    policy = PolicyModel.model_validate(yaml.safe_load(raw))
    assert policy.execution.walk_profile == "aggressive_fast"
    assert policy.execution.walk_profiles["aggressive_fast"] == [0.01, 0.03, 0.06, 0.10]
    assert policy.execution.max_chase_pct == pytest.approx(0.15)
    assert policy.execution.reprice_interval_ms == 2500
```

- [ ] **Step 2: Run failing test**

```
pytest tests/unit/test_policy.py::test_walk_profiles_loaded -v
```

Expected: FAIL — fields not on ExecutionPolicy.

- [ ] **Step 3: Update ExecutionPolicy in agent/policy.py**

```python
class ExecutionPolicy(BaseModel):
    fill_wait_timeout_seconds: float = 30.0
    max_equity_price: float = 500.0
    reconciler_interval_seconds: int = 60
    walk_profile: str = "aggressive_fast"
    walk_profiles: dict[str, list[float]] = {
        "cautious_fast":   [0.00, 0.02, 0.05, 0.10],
        "aggressive_fast": [0.01, 0.03, 0.06, 0.10],
    }
    reprice_interval_ms: int = 2500
    max_chase_pct: float = 0.15
```

- [ ] **Step 4: Update config/policy.yaml**

Append to the existing `execution:` block:

```yaml
execution:
  fill_wait_timeout_seconds: 30
  max_equity_price: 500.0
  reconciler_interval_seconds: 60
  walk_profile: aggressive_fast
  walk_profiles:
    cautious_fast:   [0.00, 0.02, 0.05, 0.10]
    aggressive_fast: [0.01, 0.03, 0.06, 0.10]
  reprice_interval_ms: 2500
  max_chase_pct: 0.15
```

- [ ] **Step 5: Run tests**

```
pytest tests/unit/test_policy.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/policy.py config/policy.yaml tests/unit/test_policy.py
git commit -m "feat(policy): add walk_profiles, reprice_interval_ms, max_chase_pct to ExecutionPolicy"
```

---

## Task 8: Registry — Wire New Skills, Remove Old Skills

**Files:**
- Modify: `agent/registry.py`

- [ ] **Step 1: Update registry.py — Phase 1 chain**

Replace `build_phase1_chain` in `agent/registry.py`:

```python
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
```

- [ ] **Step 2: Update registry.py — Phase 2b chain**

Replace `build_phase2b_execution_chain` (from Spec 1) to use `PriceWalker` instead of `OrderSubmitter`+`FillWaiter`:

```python
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
```

- [ ] **Step 3: Update main.py — pass gateway to Phase 1 chain**

In `main.py`, update the `build_phase1_chain` call:

```python
    phase1_chain = build_phase1_chain(policy, idempotency_store, telegram, gateway=gateway)
```

- [ ] **Step 4: Run full test suite**

```
pytest --tb=short -q
```

Expected: all PASS. Any test that directly instantiates `TradeIntentDetector`, `TickerResolver`, or `ConvictionClassifier` through the registry will fail — fix by updating them to use `SignalAnalyzer`.

- [ ] **Step 5: Commit**

```bash
git add agent/registry.py main.py
git commit -m "feat(registry): wire SignalAnalyzer+TickerValidator; replace OrderSubmitter+FillWaiter with PriceWalker"
```

---

## Task 9: E2E Test — PriceWalker execution pipeline

**Files:**
- Modify: `tests/e2e/test_phase2b_execution_pipeline.py`

- [ ] **Step 1: Add PriceWalker e2e test**

Add to `tests/e2e/test_phase2b_execution_pipeline.py`:

```python
from skills.execution.price_walker import PriceWalker
from infra.storage.trade_intent_store import TradeIntentStore


def _gateway_with_price_walker():
    fake_trade = MagicMock()
    fake_trade.order.orderId = "IB-PW-1"
    fake_trade.order.permId = 99
    fill_status = MagicMock()
    fill_status.status = "Filled"
    fill_status.filled = 9
    fill_status.remaining = 0
    fill_status.avgFillPrice = 5.56
    fake_trade.orderStatus = fill_status

    gw = MagicMock()
    gw.get_chain = AsyncMock(return_value=[_candidate(strike=150.0)])
    gw.get_account_summary = AsyncMock(return_value=AccountSummary(
        buying_power=50_000.0, net_liquidation=50_000.0, currency="USD"
    ))
    gw.get_quote = AsyncMock(return_value=155.0)
    gw.qualify = AsyncMock(side_effect=lambda ref: setattr(ref, 'qualified', True) or ref)
    gw.place_order = AsyncMock(return_value=fake_trade)
    gw.cancel_order = AsyncMock(return_value=True)
    gw.get_option_ask = AsyncMock(return_value=(5.50, 0.0))
    gw._account_id = "DU12345"
    return gw


@pytest.mark.asyncio
async def test_price_walker_fills_on_first_step(db):
    policy = _policy()
    # Add walk profile fields expected by PriceWalker
    policy.execution.walk_profile = "aggressive_fast"
    policy.execution.walk_profiles = {
        "aggressive_fast": [0.01, 0.03, 0.06, 0.10],
    }
    policy.execution.max_chase_pct = 0.15
    policy.execution.reprice_interval_ms = 100  # fast for tests

    gateway = _gateway_with_price_walker()
    execution_store = ExecutionStore(db)
    intent_store = TradeIntentStore(db)
    trace_store = TraceStore(db)

    chain = [
        ExecutionEligibilityGuard(policy, time_fn=_rth_time),
        ChainLookup(gateway, db),
        InstrumentMarketabilityGuard(policy),
        ContractSelector(policy),
        OrderSizer(policy, gateway),
        OrderPricer(policy),
        PriceWalker(policy, gateway, intent_store),
    ]

    orch = Orchestrator(chain, trace_store)
    ctx = Context(trace_id=str(uuid.uuid4())[:12], event_id="evt-pw-1")
    ctx.update({
        "signal_id": "sig-pw-1",
        "ticker": "AAPL",
        "conviction_bucket": "high",
        "spot_price": 152.0,
        "intent_id": "evt-pw-1:AAPL:long",
        "action": "BUY",
    })

    result_ctx = await orch.run(ctx)
    assert result_ctx.get("fill_status") == "filled"
    assert result_ctx.get("order_attempt_count") == 1
```

- [ ] **Step 2: Run e2e tests**

```
pytest tests/e2e/test_phase2b_execution_pipeline.py -v
```

Expected: all PASS including new PriceWalker test.

- [ ] **Step 3: Run full test suite**

```
pytest --tb=short -q
```

Expected: all PASS with no regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_phase2b_execution_pipeline.py
git commit -m "test(e2e): add PriceWalker fill e2e test"
```

---

## Spec 2 Complete

Both specs are now implemented:
- Spec 1 backbone: durable `trade_intents` record, two-track policy/execution state, outbox reconciliation
- Spec 2 speed: single Haiku `SignalAnalyzer`, pre-filtered parallel `get_chain()`, AX-first `DesktopReader`, bounded `PriceWalker`

Signal-to-order path is now: AX text → 1 Haiku call → deterministic validation → 4–6 parallel contract qualifies → PriceWalker step 1 live.
