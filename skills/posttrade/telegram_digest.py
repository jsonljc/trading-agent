import asyncio
import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class TelegramDigest(Skill):
    name = "telegram_digest"

    def __init__(self, client, mode: str = "signal_only") -> None:
        self._client = client
        self._mode = mode
        self._pending: set[asyncio.Task] = set()

    async def run(self, ctx: Context) -> SkillResult:
        # The "signal parsed" digest must NOT block order execution. The send is
        # an httpx round-trip (up to a 10s timeout) that previously sat on the
        # critical path BEFORE the order was placed. Fire it concurrently and
        # return immediately so phase2b execution proceeds without waiting; the
        # in-flight send is flushed on shutdown via drain().
        try:
            text = self._format_signal_digest(ctx)
        except Exception as exc:
            logger.error("telegram_digest format failed: %s", exc)
            return SkillResult(
                status="success",
                updates={"digest_failure": str(exc)},
                reason=f"telegram digest format failed: {exc}",
            )
        self._spawn(self._client.send_message(text))
        return SkillResult(status="success")

    def _spawn(self, coro) -> None:
        """Run a Telegram send concurrently, holding a reference so it is not
        garbage-collected mid-flight and its failures are logged, not lost."""
        task = asyncio.create_task(self._guarded_send(coro))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _guarded_send(self, coro) -> None:
        try:
            await coro
        except Exception as exc:
            logger.error("telegram_digest send failed: %s", exc)

    async def drain(self) -> None:
        """Await any in-flight background sends — call on graceful shutdown so a
        just-fired signal digest isn't dropped."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def _format_signal_digest(self, ctx: Context) -> str:
        import html
        message = html.escape(ctx.get("full_message_text", "?"))
        channel = html.escape(ctx.get("channel", "?"))
        author = html.escape(ctx.get("author", "?"))
        ticker = html.escape(ctx.get("ticker", "unresolved"))
        confidence_val = ctx.get("confidence", "?")
        if isinstance(confidence_val, float):
            confidence = f"{confidence_val:.2f}"
        else:
            confidence = html.escape(str(confidence_val))
        bucket = html.escape(ctx.get("bucket", "?"))
        return (
            f"<b>SIGNAL PARSED</b>\n\n"
            f"Source: #{channel}\n"
            f"Author: {author}\n"
            f"Message: <i>{message}</i>\n\n"
            f"Confidence: {confidence}\n"
            f"Ticker: <b>{ticker}</b>\n"
            f"Bucket: {bucket}\n\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )

    # Markers in skip reasons that indicate the broker (IB) was unreachable.
    # When any of these appear on an actionable bucket (HIGH/LOW) skip, the
    # signal is silently dropped — we surface it via send_missed_signal_alert.
    _BROKER_UNAVAILABLE_MARKERS = (
        "circuit open",
        "broker_unavailable",
        "could not be validated",
    )

    @classmethod
    def is_broker_unavailable_skip(cls, ctx: Context, reason: str) -> bool:
        bucket = ctx.get("bucket")
        if bucket not in ("HIGH", "LOW"):
            return False
        return any(m in reason for m in cls._BROKER_UNAVAILABLE_MARKERS)

    # size_source values the TraderClassifier stamps on ctx when it DEGRADED or
    # DROPPED a probable entry — as opposed to a genuine commentary SKIP
    # (size_source="skip"), which is the common, correct case and MUST stay
    # silent. The skip reason that actually reaches on_skip for all of these is
    # EntrySkipGate's generic "no_entry:bucket=SKIP", so the discriminator is the
    # size_source, NOT the reason string.
    _MISSED_ENTRY_SIZE_SOURCES = (
        "llm_error",          # classifier raised → forced SKIP (we lost the read)
        "drop_low_conf",      # actionable-looking entry below the confidence floor
        "ticker_not_in_msg",  # anti-hallucination drop of an LLM-invented ticker
    )

    # Skip-reason prefixes (emitted upstream of / instead of the classifier) that
    # also indicate a probable real entry was dropped.
    _MISSED_ENTRY_REASON_PREFIXES = (
        "no_trader_profile:",  # TraderRouter: unknown author on a tracked channel
        "entry_outside_rth",   # RthEntryGuard: actionable entry fired off-session
    )

    @classmethod
    def is_missed_entry_skip(cls, ctx: Context, reason: str) -> bool:
        """True when a skip likely dropped a REAL entry (degraded/missed) and so
        should be surfaced to the operator via send_missed_signal_alert.

        Mirrors is_broker_unavailable_skip; consumed by main.on_skip. Returns
        False for a genuine commentary SKIP (size_source="skip") and for benign
        filters (bot_author, missing_alert_mention, dedup, idempotency) so we do
        not spam the operator on the common, correct cases.
        """
        if ctx.get("size_source") in cls._MISSED_ENTRY_SIZE_SOURCES:
            return True
        return bool(reason) and reason.startswith(cls._MISSED_ENTRY_REASON_PREFIXES)

    @classmethod
    def missed_entry_reason(cls, ctx: Context, reason: str) -> str:
        """Operator-facing reason for a missed-entry alert.

        For classifier drops the raw skip reason is the uninformative
        "no_entry:bucket=SKIP"; surface the classifier's own size_source +
        reason instead so the alert says WHY. Upstream skips (no_trader_profile,
        entry_outside_rth) already carry a descriptive reason, so pass through.
        """
        size_source = ctx.get("size_source")
        if size_source in cls._MISSED_ENTRY_SIZE_SOURCES:
            detail = ctx.get("classifier_reason")
            return f"{size_source} — {detail}" if detail else size_source
        return reason

    @staticmethod
    def is_order_rejected(reason: str) -> bool:
        """A hard broker rejection (distinct from a fill timeout / broker-down)."""
        return (reason.startswith(("shares_rejected", "options_rejected"))
                or "broker_rejected" in reason)

    async def send_order_rejected_alert(self, ctx: Context, reason: str) -> None:
        import html
        ticker = html.escape(ctx.get("ticker") or "?")
        side = html.escape(ctx.get("side") or "?")
        text = (
            f"🛑 <b>ORDER REJECTED</b>\n\n"
            f"Ticker: <b>{ticker}</b> {side}\n"
            f"Reason: {html.escape(reason)}\n"
            f"The broker rejected the order (not a timeout) — it is in the DLQ "
            f"for review; nothing was filled.\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Order-rejected alert delivery failed: %s", exc)

    async def send_missed_signal_alert(self, ctx: Context, reason: str) -> None:
        import html
        ticker = html.escape(ctx.get("ticker") or "?")
        side = html.escape(ctx.get("side") or "?")
        trader = html.escape(ctx.get("trader_handle") or "?")
        bucket = html.escape(ctx.get("bucket") or "?")
        text = (
            f"⚠️ <b>MISSED SIGNAL</b>\n\n"
            f"Trader: {trader}\n"
            f"Ticker: <b>{ticker}</b> {side} ({bucket})\n"
            f"Reason: {html.escape(reason)}\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Missed-signal alert delivery failed: %s", exc)

    async def send_error_digest(self, ctx: Context, reason: str) -> None:
        import html
        text = (
            f"<b>ERROR</b>\n\n"
            f"Reason: {html.escape(reason)}\n"
            f"Channel: #{html.escape(ctx.get('channel', '?'))}\n"
            f"Preview: <i>{html.escape(ctx.get('trigger_preview', '?'))}</i>\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Error digest delivery failed: %s", exc)

    async def send_sell_digest(self, ctx: Context) -> None:
        import html
        qty = ctx.get("sell_total_sold_qty")
        if not qty:
            return  # nothing actually sold -> no success digest (alerted elsewhere)
        ticker = html.escape(ctx.get("sell_ticker") or ctx.get("ticker") or "?")
        scope = html.escape(ctx.get("sell_scope") or "?")
        trader = html.escape(ctx.get("trader_handle") or ctx.get("channel") or "?")
        text = (
            f"✅ <b>FOLLOWED SELL</b>\n\n"
            f"Trader: {trader}\n"
            f"Ticker: <b>{ticker}</b> — sold {qty} shares ({scope})\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Sell digest delivery failed: %s", exc)

    async def send_fill_digest(self, ctx: Context) -> None:
        import html
        ticker = html.escape(ctx.get("ticker", "?"))
        channel = html.escape(ctx.get("channel", "?"))

        shares_qty = ctx.get("shares_fill_qty")
        shares_px = ctx.get("shares_fill_price")
        options_qty = ctx.get("options_fill_qty")
        options_px = ctx.get("options_fill_price")
        partial_reason = ctx.get("partial_execution_reason")

        if shares_qty is None and options_qty is None:
            return  # nothing to report (chain ended before any fill)

        lines: list[str] = []
        if shares_qty is not None:
            shares_px_str = f"${shares_px:.2f}" if shares_px else "pending"
            lines.append(f"✅ SHARES: {shares_qty} @ {shares_px_str}")
        if options_qty is not None:
            opt_px_str = f"${options_px:.2f}" if options_px else "pending"
            strike = ctx.get("selected_strike")
            expiry = html.escape(str(ctx.get("selected_expiry", "")))
            lines.append(f"✅ OPTIONS: {options_qty}× C{strike} {expiry} @ {opt_px_str}")
        elif partial_reason:
            lines.append(f"⚠️ OPTIONS skipped: {html.escape(partial_reason)}")

        text = (
            f"<b>ORDER FILLED</b>\n\n"
            f"Ticker: <b>{ticker}</b>\n"
            + "\n".join(lines)
            + f"\nSource: #{channel}\n"
            + f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Fill digest delivery failed: %s", exc)

