import logging
from agent.context import Context, SkillResult
from agent.skill import Skill

logger = logging.getLogger(__name__)


class TelegramDigest(Skill):
    name = "telegram_digest"

    def __init__(self, client, mode: str = "signal_only") -> None:
        self._client = client
        self._mode = mode

    async def run(self, ctx: Context) -> SkillResult:
        try:
            text = self._format_signal_digest(ctx)
            await self._client.send_message(text)
            return SkillResult(status="success")
        except Exception as exc:
            logger.error("telegram_digest failed: %s", exc)
            return SkillResult(
                status="success",
                updates={"digest_failure": str(exc)},
                reason=f"telegram delivery failed: {exc}",
            )

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
        ticker = html.escape(ctx.get("sell_ticker") or ctx.get("ticker") or "?")
        qty = ctx.get("sell_total_sold_qty")
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

