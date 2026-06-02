from __future__ import annotations
import asyncio
import logging

from infra.ib.gateway import IBGatewayUnavailable

logger = logging.getLogger(__name__)

# Order refs we set look like "<trace>:shares:<event>" / ":options:" / ":trim:".
_OURS_MARKERS = (":shares:", ":options:", ":trim:")


class ExecutionReconciler:
    """Diffs live broker state against agent.db on startup, each loop, and after
    each reconnect, so orders that completed (or vanished) while the process was
    down are surfaced for manual review.

    It NEVER auto-resubmits or auto-cancels — reconciliation only detects and
    alerts. Two discrepancy classes:
      * vanished: a db intent in 'submitted'/'dispatched' whose broker order is no
        longer working at IB (filled / cancelled / rejected while we were down).
      * orphan: a live IB open order that looks like ours but maps to no in-flight
        intent.
    """

    def __init__(self, gateway, execution_store, trade_intent_store=None,
                 interval_seconds: int = 60, on_discrepancy=None) -> None:
        self._gateway = gateway
        self._intent_store = trade_intent_store
        self._interval = interval_seconds
        self._on_discrepancy = on_discrepancy  # async callable(summary: dict)
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("ExecutionReconciler started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # Reconcile immediately on startup, then on every interval.
        while not self._stopping:
            try:
                await self._reconcile_intents()
            except Exception as exc:
                logger.exception("ExecutionReconciler error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _reconcile_intents(self) -> None:
        summary = await self.reconcile_once()
        if summary is None:
            return
        for v in summary["vanished"]:
            logger.warning(
                "ExecutionReconciler: intent %s (%s) broker order %s no longer "
                "working at IB; in_position=%s — MANUAL REVIEW",
                v["intent_id"], v["ticker"], v["broker_order_ref"], v["in_position"],
            )
        for o in summary["orphans"]:
            logger.warning(
                "ExecutionReconciler: orphan IB open order %s (ref=%s) — no "
                "matching in-flight intent. MANUAL REVIEW",
                o["order_id"], o["order_ref"],
            )

    async def reconcile_once(self) -> dict | None:
        """Build the discrepancy summary. Returns None if the broker is
        unreachable (can't reconcile while down — try again next loop)."""
        if self._intent_store is None:
            return None
        pending = await self._intent_store.get_pending_outbox()
        try:
            open_orders = await self._gateway.get_open_orders()
        except IBGatewayUnavailable:
            logger.info("ExecutionReconciler: broker unavailable, skipping pass")
            return None

        live_ids = {str(getattr(o, "orderId", "")) for o in open_orders}
        pos_symbols = await self._live_position_symbols()
        pending_refs = {r["broker_order_ref"] for r in pending if r["broker_order_ref"]}

        vanished = []
        for r in pending:
            ref = r["broker_order_ref"]
            if ref and ref not in live_ids:
                vanished.append({
                    "intent_id": r["intent_id"],
                    "ticker": r["ticker"],
                    "broker_order_ref": ref,
                    "in_position": r["ticker"] in pos_symbols,
                })

        orphans = []
        for o in open_orders:
            oid = str(getattr(o, "orderId", ""))
            oref = getattr(o, "orderRef", "") or ""
            if oid not in pending_refs and any(m in oref for m in _OURS_MARKERS):
                orphans.append({"order_id": oid, "order_ref": oref})

        summary = {"vanished": vanished, "orphans": orphans}
        if (vanished or orphans) and self._on_discrepancy is not None:
            try:
                await self._on_discrepancy(summary)
            except Exception:
                logger.exception("reconciler discrepancy alert failed")
        return summary

    async def _live_position_symbols(self) -> set[str]:
        """Symbols with a non-zero live position. Best-effort: a gateway without
        positions support (or a transient error) yields an empty set rather than
        aborting the whole reconcile pass."""
        try:
            positions = await self._gateway.get_positions()
        except Exception:
            return set()
        symbols = set()
        for p in positions:
            try:
                if p.position != 0:
                    symbols.add(p.contract.symbol)
            except Exception:
                continue
        return symbols
