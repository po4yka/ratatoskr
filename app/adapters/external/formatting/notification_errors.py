"""Error-notification builders and dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.telegram.telethon_compat import InlineKeyboardButton, InlineKeyboardMarkup
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.ui_strings import t

if TYPE_CHECKING:
    from .notification_context import NotificationFormatterContext

logger = get_logger(__name__)


def _build_retry_markup(correlation_id: str) -> InlineKeyboardMarkup | None:
    """Build an inline keyboard with a Retry button for failed summaries."""
    if not correlation_id:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="Retry",
                    callback_data=f"retry:{correlation_id}",
                )
            ]
        ]
    )


class NotificationErrorPresenter:
    """Render rich user-facing error notifications."""

    def __init__(self, context: NotificationFormatterContext) -> None:
        self._context = context

    async def send_error_notification(
        self,
        message: Any,
        error_type: str,
        correlation_id: str,
        details: str | None = None,
        reply_markup: Any | None = None,
    ) -> None:
        if correlation_id and correlation_id in self._context.notified_error_ids:
            return
        if correlation_id:
            self._context.notified_error_ids.add(correlation_id)

        try:
            error_text, should_admin_log = self._build_error_text(
                error_type=error_type,
                correlation_id=correlation_id,
                details=details,
            )
            effective_markup = reply_markup
            if effective_markup is None and error_type == "processing_failed":
                effective_markup = _build_retry_markup(correlation_id)
            await self._emit_html_error(message, error_text, reply_markup=effective_markup)
            if should_admin_log:
                await self._context.response_sender.send_to_admin_log(
                    error_text,
                    correlation_id=correlation_id,
                )
        except Exception as exc:
            logger.debug("notification_send_failed", extra={"error": str(exc)})
            raise_if_cancelled(exc)

    async def _emit_html_error(
        self, message: Any, error_text: str, *, reply_markup: Any | None = None
    ) -> None:
        if self._context.progress_tracker is not None:
            self._context.progress_tracker.clear(message)
        await self._context.response_sender.safe_reply(
            message, error_text, parse_mode="HTML", reply_markup=reply_markup
        )

    def _build_error_text(
        self,
        *,
        error_type: str,
        correlation_id: str,
        details: str | None,
    ) -> tuple[str, bool]:
        builders = {
            "firecrawl_error": self._build_firecrawl_error_text,
            "empty_content": self._build_empty_content_error_text,
            "processing_failed": self._build_processing_failed_error_text,
            "llm_error": self._build_llm_error_text,
            "unexpected_error": self._build_unexpected_error_text,
            "timeout": self._build_timeout_error_text,
            "rate_limit": self._build_rate_limit_error_text,
            "network_error": self._build_network_error_text,
            "database_error": self._build_database_error_text,
            "access_denied": self._build_access_denied_error_text,
            "access_blocked": self._build_access_blocked_error_text,
            "message_too_long": self._build_message_too_long_error_text,
            "no_urls_found": self._build_no_urls_found_error_text,
        }
        builder = builders.get(error_type, self._build_generic_error_text)
        return builder(correlation_id=correlation_id, details=details)

    def _build_firecrawl_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        details_block = f"\n\n<i>{t('details', _l)}: {details}</i>" if details else ""
        error_text = (
            f"❌ <b>{t('err_firecrawl_title', _l)}</b>\n\n"
            f"{t('err_firecrawl_body', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
            f"{details_block}\n\n"
            f"<b>{t('err_firecrawl_solutions', _l)}:</b>\n"
            f"• {t('err_firecrawl_hint_url', _l)}\n"
            f"• {t('err_firecrawl_hint_paywall', _l)}\n"
            f"• {t('err_firecrawl_hint_text', _l)}"
        )
        return error_text, True

    def _build_empty_content_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _ = details
        _l = self._context.lang
        error_text = (
            f"❌ <b>{t('err_empty_title', _l)}</b>\n\n"
            f"{t('err_empty_body', _l)}\n\n"
            f"<b>{t('err_empty_causes', _l)}:</b>\n"
            f"• {t('err_empty_cause_block', _l)}\n"
            f"• {t('err_empty_cause_paywall', _l)}\n"
            f"• {t('err_empty_cause_nontext', _l)}\n"
            f"• {t('err_empty_cause_server', _l)}\n\n"
            f"<b>{t('err_empty_suggestions', _l)}:</b>\n"
            f"• {t('err_empty_hint_url', _l)}\n"
            f"• {t('err_empty_hint_private', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
        )
        return error_text, True

    def _build_processing_failed_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        detail_block = f"\n\n<i>{t('reason', _l)}: {details}</i>" if details else ""
        error_text = (
            f"⚙️ <b>{t('err_processing_title', _l)}</b>\n\n"
            f"{t('err_processing_body', _l)}\n\n"
            f"<b>{t('err_processing_what', _l)}:</b>\n"
            f"• {t('err_processing_parse', _l)}\n"
            f"• {t('err_processing_repair', _l)}\n\n"
            f"<b>{t('err_processing_try', _l)}:</b>\n"
            f"• {t('err_processing_hint_retry', _l)}\n"
            f"• {t('err_processing_hint_other', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>{detail_block}"
        )
        return error_text, True

    def _build_llm_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        models_info = ""
        error_info = details or ""

        # New-style "all attempts failed" details from _handle_all_attempts_failed.
        if "Tried" in error_info and "model(s):" in error_info:
            lines = error_info.split("\n")
            models_info = f"\n• {lines[0]}" if lines else ""
            error_detail = "\n".join(lines[1:]) if len(lines) > 1 else ""
        # Timeout-specific message — render as a human-friendly block, not raw jargon.
        elif "per-model time budget" in error_info:
            # The message is already user-friendly; present it verbatim in an italic block.
            error_detail = f"\n\n<i>{error_info}</i>"
        else:
            error_detail = f"\n\n<i>Provider response: {details}</i>" if details else ""

        error_text = f"🤖 <b>{t('err_llm_title', _l)}</b>\n\n{t('err_llm_body', _l)}"
        if models_info:
            error_text += f"\n\n<b>{t('err_llm_models', _l)}:</b>{models_info}"
        error_text += (
            f"\n\n<b>{t('err_llm_solutions', _l)}:</b>\n"
            f"• {t('err_llm_hint_retry', _l)}\n"
            f"• {t('err_llm_hint_complex', _l)}\n"
            f"• {t('err_llm_hint_support', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
        )
        if error_detail:
            error_text += error_detail
        return error_text, True

    def _build_unexpected_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        details_block = f"\n\n<i>{t('details', _l)}: {details}</i>" if details else ""
        error_text = (
            f"⚠️ <b>{t('err_unexpected_title', _l)}</b>\n\n"
            f"{t('err_unexpected_body', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>\n"
            f"<b>{t('status', _l)}:</b> {t('err_unexpected_status', _l)}"
            f"{details_block}"
        )
        return error_text, True

    def _build_timeout_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        error_text = (
            f"⏱ <b>{t('err_timeout_title', _l)}</b>\n\n"
            f"{details or t('err_timeout_default', _l)}\n\n"
            f"<b>{t('err_timeout_try', _l)}:</b>\n"
            f"• {t('err_timeout_hint_smaller', _l)}\n"
            f"• {t('err_timeout_hint_wait', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
        )
        return error_text, True

    def _build_rate_limit_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        error_text = (
            f"⏳ <b>{t('err_rate_limit_title', _l)}</b>\n\n"
            f"{details or t('err_rate_limit_default', _l)}\n\n"
            f"<b>{t('status', _l)}:</b> {t('err_rate_limit_status', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
        )
        return error_text, True

    def _build_network_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        error_text = (
            f"🌐 <b>{t('err_network_title', _l)}</b>\n\n"
            f"{details or t('err_network_default', _l)}\n\n"
            f"<b>{t('err_network_try', _l)}:</b>\n"
            f"• {t('err_network_hint_conn', _l)}\n"
            f"• {t('err_network_hint_retry', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
        )
        return error_text, True

    def _build_database_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        error_text = (
            f"💾 <b>{t('err_database_title', _l)}</b>\n\n"
            f"{details or t('err_database_default', _l)}\n\n"
            f"<b>{t('status', _l)}:</b> {t('err_database_status', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
        )
        return error_text, True

    def _build_access_denied_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _ = correlation_id
        _l = self._context.lang
        error_text = (
            f"🛑 <b>{t('err_access_denied_title', _l)}</b>\n\n"
            f"{t('err_access_denied_body', _l).format(uid=details or 'unknown')}\n\n"
            f"{t('err_access_denied_contact', _l)}"
        )
        return error_text, False

    def _build_access_blocked_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _ = correlation_id
        _l = self._context.lang
        error_text = (
            f"🚫 <b>{t('err_access_blocked_title', _l)}</b>\n\n"
            f"{details or t('err_access_blocked_default', _l)}\n\n"
            f"<b>{t('status', _l)}:</b> {t('err_access_blocked_status', _l)}"
        )
        return error_text, False

    def _build_message_too_long_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _ = correlation_id
        _l = self._context.lang
        error_text = (
            f"📏 <b>{t('err_message_too_long_title', _l)}</b>\n\n"
            f"{details or t('err_message_too_long_default', _l)}\n\n"
            f"<b>{t('suggestions', _l)}:</b>\n"
            f"• {t('err_message_too_long_hint_split', _l)}\n"
            f"• {t('err_message_too_long_hint_file', _l)}"
        )
        return error_text, False

    def _build_no_urls_found_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _ = correlation_id
        _l = self._context.lang
        error_text = (
            f"🔗 <b>{t('err_no_urls_title', _l)}</b>\n\n"
            f"{details or t('err_no_urls_default', _l)}\n\n"
            f"<b>{t('try_label', _l)}:</b>\n"
            f"• {t('err_no_urls_hint_http', _l)}\n"
            f"• {t('err_no_urls_hint_typo', _l)}"
        )
        return error_text, False

    def _build_generic_error_text(
        self, *, correlation_id: str, details: str | None
    ) -> tuple[str, bool]:
        _l = self._context.lang
        error_text = (
            f"<b>{t('err_generic_title', _l)}</b>\n"
            f"{details or t('err_generic_default', _l)}\n\n"
            f"<b>{t('error_id', _l)}:</b> <code>{correlation_id}</code>"
        )
        return error_text, True
