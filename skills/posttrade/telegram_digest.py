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
        size_pct = ctx.get("size_pct", 0)
        pct_display = f"{size_pct * 100:.0f}%"
        message = html.escape(ctx.get("full_message_text", "?"))
        channel = html.escape(ctx.get("channel", "?"))
        author = html.escape(ctx.get("author", "?"))
        ticker = html.escape(ctx.get("ticker", "unresolved"))
        intent = html.escape(ctx.get("intent", "?"))
        confidence = html.escape(ctx.get("confidence", "?"))
        bucket = html.escape(ctx.get("bucket", "?"))
        return (
            f"<b>SIGNAL PARSED</b>\n\n"
            f"Source: #{channel}\n"
            f"Author: {author}\n"
            f"Message: <i>{message}</i>\n\n"
            f"Intent: <b>{intent}</b> ({confidence} confidence)\n"
            f"Ticker: <b>{ticker}</b>\n"
            f"Bucket: {bucket} → {pct_display} allocation\n\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )

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

    async def send_fill_digest(self, ctx: Context) -> None:
        import html
        fill_status = ctx.get("fill_status", "?")
        ticker = html.escape(ctx.get("ticker", "?"))
        filled_qty = ctx.get("filled_qty", "?")
        avg_price = ctx.get("avg_fill_price")
        price_str = f"${avg_price:.2f}" if avg_price else "pending"
        instrument = ctx.get("instrument_type", "?")
        channel = html.escape(ctx.get("channel", "?"))
        perm_id = ctx.get("perm_id", "")
        status_emoji = {"FILLED": "✅", "TIMED_OUT_PENDING": "⏳", "PARTIAL_FILL": "⚡"}.get(fill_status, "📋")
        text = (
            f"{status_emoji} <b>ORDER {fill_status}</b>\n\n"
            f"Ticker: <b>{ticker}</b> ({instrument})\n"
            f"Qty: {filled_qty} @ {price_str}\n"
            f"Source: #{channel}\n"
        )
        if perm_id:
            text += f"PermID: <code>{perm_id}</code>\n"
        text += f"<code>trace: {ctx.trace_id}</code>"
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Fill digest delivery failed: %s", exc)

    async def send_skip_digest(self, ctx: Context, reason: str) -> None:
        import html
        text = (
            f"<b>SKIPPED</b>\n\n"
            f"Reason: {html.escape(reason)}\n"
            f"Channel: #{html.escape(ctx.get('channel', '?'))}\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Skip digest delivery failed: %s", exc)

    async def send_bootstrap_review_digest(self, ctx: Context) -> None:
        import html
        trader = html.escape(ctx.get("trader_handle", "?"))
        author = html.escape(ctx.get("author", "?"))
        channel = html.escape(ctx.get("channel", "?"))
        ticker = html.escape(ctx.get("ticker") or "(no-ticker)")
        bucket = html.escape(ctx.get("bucket", "?"))
        size_pct = ctx.get("size_pct", 0.0)
        size_display = f"{size_pct * 100:.0f}%"
        confidence = ctx.get("confidence", 0.0)
        why = html.escape(ctx.get("classifier_reason", ""))
        msg = html.escape(ctx.get("full_message_text", ""))
        text = (
            f"<b>BOOTSTRAP REVIEW</b>\n\n"
            f"Trader: {trader} ({author})\n"
            f"Channel: #{channel}\n"
            f"Ticker: <b>{ticker}</b>\n"
            f"Proposed: <b>{bucket}</b> @ {size_display} (conf {confidence:.2f})\n"
            f"Why: <i>{why}</i>\n\n"
            f"Message:\n<i>{msg}</i>\n\n"
            f"<code>trace: {ctx.trace_id}</code>"
        )
        try:
            await self._client.send_message(text)
        except Exception as exc:
            logger.error("Bootstrap review delivery failed: %s", exc)
