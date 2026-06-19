"""Summary-centric callback handlers for Telegram inline actions."""

from __future__ import annotations

import asyncio
import html
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.core.ui_strings import t

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.callback_action_presenters import CallbackActionPresenters
    from app.adapters.telegram.callback_action_store import CallbackActionStore
    from app.adapters.telegram.url_handler import URLHandler
    from app.infrastructure.search.hybrid_search_service import HybridSearchService

logger = get_logger(__name__)


class CallbackActionSummaryHandlers:
    """Handle callback actions centered on existing summary data."""

    def __init__(
        self,
        *,
        response_formatter: ResponseFormatter,
        store: CallbackActionStore,
        presenters: CallbackActionPresenters,
        url_handler: URLHandler | None = None,
        hybrid_search: HybridSearchService | None = None,
        lang: str = "en",
        llm_timeout: float = 120.0,
        search_timeout: float = 30.0,
        asyncio_module: Any = asyncio,
    ) -> None:
        self._response_formatter = response_formatter
        self._store = store
        self._presenters = presenters
        self._url_handler = url_handler
        self._hybrid_search = hybrid_search
        self._lang = lang
        self._llm_timeout = llm_timeout
        self._search_timeout = search_timeout
        self._asyncio = asyncio_module
        self._summary_loader: Callable[..., Awaitable[dict[str, Any] | None]] = (
            self._store.load_summary_payload
        )

    def bind_summary_loader(
        self,
        summary_loader: Callable[..., Awaitable[dict[str, Any] | None]],
    ) -> None:
        self._summary_loader = summary_loader

    async def handle_translate(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 2:
            return False

        summary_id = ":".join(parts[1:]).strip()
        summary_data = await self._summary_loader(
            summary_id,
            correlation_id=correlation_id,
        )
        if not summary_data:
            await self._response_formatter.safe_reply(
                message, t("cb_summary_not_found", self._lang)
            )
            return True

        if summary_data.get("lang") == "ru":
            await self._response_formatter.safe_reply(
                message,
                t("cb_translation_already_ru", self._lang),
            )
            return True

        if not self._url_handler:
            await self._response_formatter.send_error_notification(
                message,
                "unexpected_error",
                correlation_id,
                details="Translation service is temporarily unavailable.",
            )
            return True

        await self._response_formatter.safe_reply(
            message,
            t("cb_translation_processing", self._lang),
        )

        try:
            request_id = summary_data.get("request_id")
            if not isinstance(request_id, int):
                raise ValueError("Invalid request ID for translation")

            translated_text = await self._asyncio.wait_for(
                self._url_handler.translate_summary_to_ru(
                    summary=summary_data,
                    req_id=request_id,
                    correlation_id=correlation_id,
                    source_lang=summary_data.get("lang"),
                ),
                timeout=self._llm_timeout,
            )

            if translated_text:
                await self._response_formatter.send_russian_translation(
                    message,
                    translated_text,
                    correlation_id=correlation_id,
                )
            else:
                await self._response_formatter.safe_reply(
                    message,
                    "Translation failed to generate meaningful output.",
                )
        except TimeoutError:
            logger.warning(
                "translation_timeout",
                extra={"summary_id": summary_id, "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(message, t("cb_timeout", self._lang))
        except Exception as exc:
            logger.exception(
                "translation_failed",
                extra={"summary_id": summary_id, "error": str(exc), "cid": correlation_id},
            )
            await self._response_formatter.send_error_notification(
                message,
                "unexpected_error",
                correlation_id,
                details="An error occurred during translation.",
            )

        logger.info(
            "translate_completed",
            extra={"summary_id": summary_id, "uid": uid, "cid": correlation_id},
        )
        return True

    async def handle_find_similar(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 2:
            return False

        summary_id = ":".join(parts[1:]).strip()
        summary_data = await self._summary_loader(
            summary_id,
            correlation_id=correlation_id,
        )
        if not summary_data:
            await self._response_formatter.safe_reply(
                message, t("cb_summary_not_found", self._lang)
            )
            return True

        if not self._hybrid_search:
            await self._response_formatter.safe_reply(
                message, t("cb_search_unavailable", self._lang)
            )
            return True

        query = self._presenters.build_similar_query(summary_data)
        if not query:
            await self._response_formatter.safe_reply(message, t("cb_not_enough_info", self._lang))
            return True

        title = str((summary_data.get("metadata") or {}).get("title") or "")
        await self._response_formatter.safe_reply(
            message,
            f"🔍 {t('cb_finding_similar', self._lang).format(title=html.escape(title or 'this item'))}",
            parse_mode="HTML",
        )

        try:
            results = await self._asyncio.wait_for(
                self._hybrid_search.search(query, correlation_id=correlation_id),
                timeout=self._search_timeout,
            )

            current_url = summary_data.get("url")
            filtered_results = [
                result for result in results if not (current_url and result.url == current_url)
            ]

            if not filtered_results:
                await self._response_formatter.safe_reply(message, t("cb_no_similar", self._lang))
            else:
                await self._response_formatter.send_topic_search_results(
                    message,
                    topic=f"Similar to: {title[:30]}...",
                    articles=filtered_results,
                    source="hybrid",
                )
        except TimeoutError:
            logger.warning(
                "find_similar_timeout",
                extra={"summary_id": summary_id, "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(message, t("cb_timeout", self._lang))
        except Exception as exc:
            logger.exception(
                "find_similar_failed",
                extra={"summary_id": summary_id, "error": str(exc), "cid": correlation_id},
            )
            await self._response_formatter.send_error_notification(
                message,
                "unexpected_error",
                correlation_id,
                details="An error occurred while searching for similar content.",
            )

        logger.info(
            "find_similar_completed",
            extra={"summary_id": summary_id, "uid": uid, "cid": correlation_id},
        )
        return True

    async def handle_toggle_save(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 2:
            return False

        summary_id = ":".join(parts[1:]).strip()

        try:
            new_state = await self._store.toggle_save(summary_id, uid)

            if new_state is not None:
                status_msg = t("cb_saved", self._lang) if new_state else t("cb_removed", self._lang)
                await self._response_formatter.safe_reply(message, status_msg)
                logger.info(
                    "summary_favorite_toggled",
                    extra={
                        "summary_id": summary_id,
                        "is_favorited": new_state,
                        "uid": uid,
                        "cid": correlation_id,
                    },
                )
            else:
                await self._response_formatter.safe_reply(
                    message,
                    t("cb_summary_not_found", self._lang),
                )
        except Exception as exc:
            logger.exception(
                "toggle_save_failed",
                extra={"summary_id": summary_id, "error": str(exc), "cid": correlation_id},
            )
            await self._response_formatter.safe_reply(
                message,
                f"Failed to update favorite status. Error ID: {correlation_id}",
            )

        return True

    async def handle_rate(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 3:
            return False

        summary_id = ":".join(parts[1:-1]).strip()
        if not summary_id:
            return False

        try:
            rating = int(parts[-1])
        except ValueError:
            return False

        rating_text = (
            t("cb_feedback_positive", self._lang)
            if rating > 0
            else t("cb_feedback_negative", self._lang)
        )
        await self._response_formatter.safe_reply(
            message,
            t("cb_feedback_thanks", self._lang).format(rating=rating_text),
        )
        logger.info(
            "summary_rated",
            extra={
                "summary_id": summary_id,
                "rating": rating,
                "uid": uid,
                "cid": correlation_id,
            },
        )
        return True

    async def handle_more(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        if len(parts) < 2:
            return False

        summary_id = ":".join(parts[1:]).strip()
        summary_data = await self._summary_loader(
            summary_id,
            correlation_id=correlation_id,
        )
        if not summary_data:
            await self._response_formatter.safe_reply(
                message, t("cb_summary_not_found", self._lang)
            )
            return True

        text = self._presenters.render_more_details(summary_data)
        await self._response_formatter.safe_reply(message, text, parse_mode="HTML")
        logger.info(
            "more_details_sent",
            extra={"summary_id": summary_id, "uid": uid, "cid": correlation_id},
        )
        return True

    async def handle_show_related_summary(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        _ = uid
        if len(parts) < 2:
            return False

        try:
            request_id = int(parts[1])
        except (ValueError, IndexError):
            return False

        summary_data = await self._summary_loader(
            f"req:{request_id}",
            correlation_id=correlation_id,
        )
        if not summary_data:
            await self._response_formatter.safe_reply(
                message, t("cb_related_not_found", self._lang)
            )
            return True

        text = self._presenters.render_related_summary(summary_data)

        from app.adapters.external.formatting.summary.action_buttons import create_inline_keyboard

        summary_id = summary_data.get("id", "")
        keyboard = create_inline_keyboard(
            summary_id,
            correlation_id=correlation_id,
            lang=self._lang,
        )
        await self._response_formatter.safe_reply(
            message,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return True
