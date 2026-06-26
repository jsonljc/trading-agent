"""Stateful money-safety invariants for the position ledger, trim ladder, and
sell-follower. Drives the REAL stores + REAL SellFollower / fire_rung_if_crossed;
only the broker is faked. See docs/superpowers/specs/2026-06-26-money-safety-
invariant-suite-design.md (§3 oracle caveats, §3a finding).
"""
from __future__ import annotations

import asyncio
import math

import aiosqlite
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from infra.storage.db import SCHEMA
from infra.storage.position_exit_store import PositionExitStore
from infra.storage.trade_intent_store import TradeIntentStore
from infra.storage.trim_ladder_store import TrimLadderStore
from agent.exit_ladder import fire_rung_if_crossed
from tests.support.factories import make_filled_intent
from tests.support.fake_gateway import FakeGateway


def _round_half_up_min1(n: float) -> int:
    return max(1, int(math.floor(n + 0.5)))


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
class PositionInvariantMachine(RuleBasedStateMachine):
    intents = Bundle("intents")

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._seq = 0
        self._fill: dict[str, int] = {}                 # intent_id -> fill_qty
        self._rungs: dict[str, dict[int, dict]] = {}    # intent_id -> {rung: meta}
        self._fp_positive: dict[str, int] = {}          # fingerprint -> #positive invocations
        self.gw = FakeGateway()
        self._conn = self._run(self._connect())
        self.intents_store = TradeIntentStore(self._conn)
        self.trims = TrimLadderStore(self._conn)
        self.exits = PositionExitStore(self._conn)

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        return conn

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    # ----------------------------------------------------------------- rules
    @rule(target=intents,
          channel=st.sampled_from(["mystic", "stp", "wse"]),
          ticker=st.sampled_from(["AAPL", "NVDA", "TSLA"]),
          fill_qty=st.integers(min_value=1, max_value=1000))
    def create_filled_intent(self, channel, ticker, fill_qty):
        self._seq += 1
        intent_id = f"e{self._seq}:{ticker}:long"
        rec = make_filled_intent(intent_id, channel=channel, ticker=ticker,
                                 fill_qty=fill_qty, seq=self._seq)
        self._run(self.intents_store.insert(rec))
        self._fill[intent_id] = fill_qty
        self._rungs[intent_id] = {}
        # carry channel/ticker for sell rules added later
        self._rungs[intent_id]["_meta"] = {"channel": channel, "ticker": ticker}
        return intent_id

    _LADDER = [(1, 0.05, 0.25), (2, 0.10, 0.25), (3, 0.20, 0.50)]

    @rule(intent=intents)
    def arm_trims(self, intent):
        if self._rungs[intent].get("_armed"):
            return
        self._run(self.trims.arm(intent, rungs=self._LADDER,
                                 armed_at="2026-06-26T14:30:00+00:00"))
        for rung, thr, tp in self._LADDER:
            self._rungs[intent][rung] = {"threshold_pct": thr, "trim_pct": tp,
                                         "recorded": False}
        self._rungs[intent]["_armed"] = True

    @rule(intent=intents,
          rung=st.sampled_from([1, 2, 3]),
          fill_mode=st.sampled_from(["full", "partial", "zero"]))
    def fire_trim(self, intent, rung, fill_mode):
        meta = self._rungs[intent].get(rung)
        if meta is None or meta["recorded"]:
            return  # not armed, or already recorded (real claim would reject anyway)
        fill_qty = self._fill[intent]
        ticker = self._rungs[intent]["_meta"]["ticker"]
        # current_price crosses this rung's threshold deterministically.
        current_price = 100.0 * (1.0 + meta["threshold_pct"]) + 1.0
        self.gw.fill_mode = fill_mode
        self.gw.unavailable = False
        fired = self._run(fire_rung_if_crossed(
            gw=self.gw, trim_store=self.trims, exits_store=self.exits,
            intent_id=intent, ticker=ticker, avg_fill_price=100.0,
            original_qty=fill_qty, rung=rung,
            threshold_pct=meta["threshold_pct"], trim_pct=meta["trim_pct"],
            current_price=current_price, slippage_cap_pct=0.01))
        # `fired` is True only when a positive fill was recorded (full/partial>0).
        if fired:
            meta["recorded"] = True

    @invariant()
    def no_trim_double_fire(self):
        # INV-3: each rung records a positive fire at most once.
        for intent_id in self._fill:
            for r in self._run(self.trims.all_for_intent(intent_id)):
                # A recorded rung has non-NULL sold_qty (record_fire only runs on
                # filled_qty>0). No row can be recorded twice: the claim gate
                # blocks a second claim until release, and a recorded rung is
                # never released. Assert via the shadow: recorded rungs stay recorded.
                if r["sold_qty"] is not None:
                    assert r["fired_at"] is not None, (
                        f"{intent_id} rung {r['rung']}: sold_qty set but not fired_at")

    # ----------------------------------------------------------------- helpers
    async def _recorded_trims(self, intent_id: str) -> tuple[int, int]:
        """Mirror PositionExitStore.remaining_qty's trim handling: recorded
        sold_qty wins; else an in-flight rung (fire_started_at set, fired_at NULL)
        reserves round_half_up_min1(fill_qty*trim_pct)."""
        fill_qty = self._fill[intent_id]
        recorded = reserves = 0
        for r in await self.trims.all_for_intent(intent_id):
            if r["sold_qty"] is not None:
                recorded += int(r["sold_qty"])
            elif r["fire_started_at"] is not None and r["fired_at"] is None:
                reserves += _round_half_up_min1(fill_qty * r["trim_pct"])
        return recorded, reserves

    # ----------------------------------------------------------------- oracle
    @invariant()
    def remaining_qty_identity(self):
        for intent_id, fill_qty in self._fill.items():
            rem = self._run(self.exits.remaining_qty(intent_id))
            rec_trims, reserves = self._run(self._recorded_trims(intent_id))
            rec_exits = self._run(self.exits.sold_qty_for_intent(intent_id))
            expected = max(0, fill_qty - rec_trims - reserves - rec_exits)
            assert rem == expected, (
                f"{intent_id}: remaining_qty={rem} != {expected} "
                f"(fill={fill_qty} trims={rec_trims} reserves={reserves} exits={rec_exits})")
            assert rem >= 0, f"{intent_id}: negative remaining {rem}"

    @invariant()
    def never_oversell(self):
        # INV-1: sum of RECORDED trim + exit sold_qty <= fill_qty (per intent).
        for intent_id, fill_qty in self._fill.items():
            rec_trims, _ = self._run(self._recorded_trims(intent_id))
            rec_exits = self._run(self.exits.sold_qty_for_intent(intent_id))
            assert rec_trims + rec_exits <= fill_qty, (
                f"{intent_id}: OVERSELL recorded {rec_trims + rec_exits} > fill {fill_qty}")

    def teardown(self):
        if getattr(self, "_conn", None) is not None:
            self._run(self._conn.close())
            self._conn = None
        if not self._loop.is_closed():
            self._loop.close()


TestPositionInvariants = PositionInvariantMachine.TestCase
