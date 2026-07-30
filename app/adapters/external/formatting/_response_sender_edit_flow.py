"""Edit-oriented flows for response sender."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

from ._response_sender_shared import (
    ResponseSenderSharedState,
    normalize_parse_mode,
    validate_and_truncate,
)

logger = get_logger(__name__)

# Retry configuration for edit operations
_EDIT_MAX_RETRIES: int = 2
_EDIT_BACKOFF_DELAYS: tuple[float, ...] = (0.5, 1.0)
# Telethon already sleeps FloodWaits below its own flood_sleep_threshold (60 s
# by default), so every wait that surfaces here is a long one -- 1800 s is not
# unusual. Obeying it verbatim, up to twice, held the caller's concurrency slot
# and typing task for the better part of an hour and delayed the fresh-send
# fallback by exactly as long. Past this cap the edit is abandoned rather than
# waited out: retrying sooner would only earn the same FloodWait again.
_EDIT_MAX_FLOOD_WAIT_SEC: float = 30.0


class ResponseSenderEditFlow:
    """Handle edit and edit-or-send flows."""

    def __init__(
        self,
        state: ResponseSenderSharedState,
        *,
        safe_reply_with_id: Any,
    ) -> None:
        self._state = state
        self._safe_reply_with_id = safe_reply_with_id

    async def edit_or_send(
        self,
        message: Any,
        text: str,
        message_id: int | None = None,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if message_id and chat_id:
            edit_success = await self.edit_message(
                chat_id,
                message_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
            if edit_success:
                return message_id
            logger.debug(
                "edit_or_send_edit_failed_fallback_send",
                extra={"chat_id": chat_id, "message_id": message_id},
            )

        result = await self._safe_reply_with_id(
            message,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
        )
        return cast("int | None", result)

    @staticmethod
    def _is_retryable_edit_error(exc: Exception) -> bool:
        """Determine whether an edit failure is worth retrying.

        Retryable: network errors, timeouts, rate-limits (FloodWait / 429).
        Non-retryable: message deleted, forbidden, not-modified, etc.
        """
        exc_name = type(exc).__name__.lower()
        exc_str = str(exc).lower()

        # FloodWait / 429 -- retryable (Telegram rate-limit)
        if "floodwait" in exc_name or "too many requests" in exc_str or "429" in exc_str:
            return True

        # MessageNotModified -- not an error at all (handled separately)
        if "message is not modified" in exc_str or "message_not_modified" in exc_str:
            return False

        # Message deleted / invalid / forbidden -- not retryable
        non_retryable_signals = (
            "message_id_invalid",
            "messageidinvalid",
            "message_delete_forbidden",
            "messagedeleteforbidden",
            "forbidden",
            "message not found",
            "chat not found",
            "bot was blocked",
        )
        if any(signal in exc_str or signal in exc_name for signal in non_retryable_signals):
            return False

        # Network / timeout / connection errors -- retryable
        retryable_signals = ("network", "timeout", "connection")
        if any(signal in exc_str or signal in exc_name for signal in retryable_signals):
            return True

        # Unknown errors -- not retryable (fall back immediately)
        return False

    @staticmethod
    def _get_flood_wait_seconds(exc: Exception) -> float | None:
        """Extract the wait duration from a FloodWait-style exception.

        Telethon stores it in exc.seconds; older builds and other wrappers
        use exc.value, exc.wait_time, or exc.retry_after.
        """
        for attr in ("seconds", "value", "wait_time", "retry_after"):
            val = getattr(exc, attr, None)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
        return None

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> bool:
        text = validate_and_truncate(
            self._state,
            text,
            substitute_on_unsafe=False,
            context_log_key="edit_message",
        )
        if text is None:
            return False

        if not await self._state.validator.check_rate_limit():
            logger.warning(
                "edit_message_rate_limited",
                extra={"chat_id": chat_id, "message_id": message_id},
            )

        self._log_edit_attempt(chat_id, message_id)
        client = self._resolve_edit_client(chat_id, message_id)
        if client is None:
            return False
        if not self._validate_edit_identifiers(chat_id, message_id, text):
            return False

        last_exc: Exception | None = None
        for attempt in range(_EDIT_MAX_RETRIES + 1):
            try:
                return await self._perform_edit_message(
                    client,
                    chat_id,
                    message_id,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                )
            except Exception as exc:
                raise_if_cancelled(exc)
                last_exc = exc

                # MessageNotModified is success -- content already matches
                if self._is_message_not_modified_error(exc):
                    logger.debug(
                        "edit_message_not_modified_success",
                        extra={"chat_id": chat_id, "message_id": message_id},
                    )
                    return True

                if not self._is_retryable_edit_error(exc) or attempt >= _EDIT_MAX_RETRIES:
                    break

                # Determine backoff delay
                flood_wait = self._get_flood_wait_seconds(exc)
                if flood_wait is not None and flood_wait > _EDIT_MAX_FLOOD_WAIT_SEC:
                    logger.warning(
                        "edit_message_flood_wait_exceeds_cap",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "flood_wait": flood_wait,
                            "cap_sec": _EDIT_MAX_FLOOD_WAIT_SEC,
                        },
                    )
                    break
                delay = (
                    flood_wait
                    if flood_wait is not None
                    else _EDIT_BACKOFF_DELAYS[min(attempt, len(_EDIT_BACKOFF_DELAYS) - 1)]
                )

                logger.debug(
                    "edit_message_retry",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "attempt": attempt + 1,
                        "max_retries": _EDIT_MAX_RETRIES,
                        "delay": delay,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(delay)

        logger.warning(
            "edit_message_failed",
            exc_info=True,
            extra={
                "error": str(last_exc),
                "chat_id": chat_id,
                "message_id": message_id,
                "attempts": min(attempt + 1, _EDIT_MAX_RETRIES + 1),
            },
        )
        return False

    def _log_edit_attempt(self, chat_id: int, message_id: int) -> None:
        logger.debug(
            "edit_message_attempt",
            extra={
                "chat_id": chat_id,
                "message_id": message_id,
                "has_telegram_client": self._state.telegram_client is not None,
                "telegram_client_has_client": (
                    hasattr(self._state.telegram_client, "client")
                    if self._state.telegram_client
                    else False
                ),
                "client": (
                    self._state.telegram_client.client if self._state.telegram_client else None
                ),
            },
        )

    def _resolve_edit_client(self, chat_id: int, message_id: int) -> Any | None:
        if not self._state.telegram_client or not hasattr(self._state.telegram_client, "client"):
            logger.warning(
                "edit_message_no_telegram_client",
                extra={"chat_id": chat_id, "message_id": message_id},
            )
            return None

        client = self._state.telegram_client.client
        if not client or not hasattr(client, "edit_message_text"):
            logger.warning(
                "edit_message_no_client_method",
                extra={"chat_id": chat_id, "message_id": message_id},
            )
            return None
        return client

    @staticmethod
    def _validate_edit_identifiers(chat_id: int, message_id: int, text: str) -> bool:
        if isinstance(chat_id, int) and isinstance(message_id, int):
            return True
        logger.warning(
            "edit_message_invalid_params",
            extra={"chat_id": chat_id, "message_id": message_id, "text_length": len(text)},
        )
        return False

    @staticmethod
    def _build_edit_text(text: str, parse_mode: str | None) -> str:
        if parse_mode != "HTML" or "Updated at" in text:
            return text
        now = datetime.now(UTC).strftime("%H:%M:%S")
        return f"{text}\n\n<i>Updated at {now} UTC</i>"

    @staticmethod
    def _is_message_not_modified_error(exc: Exception | None) -> bool:
        if exc is None:
            return False
        exc_str = str(exc).lower()
        return "message is not modified" in exc_str or "message_not_modified" in exc_str

    async def _perform_edit_message(
        self,
        client: Any,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> bool:
        """Execute a single edit attempt. Raises on failure so the caller can retry."""
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": self._build_edit_text(text, parse_mode),
        }
        normalized_mode = normalize_parse_mode(parse_mode)
        if normalized_mode is not None:
            kwargs["parse_mode"] = normalized_mode
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if disable_web_page_preview is not None:
            kwargs["disable_web_page_preview"] = disable_web_page_preview

        await client.edit_message_text(**kwargs)
        logger.debug(
            "edit_message_success",
            extra={"chat_id": chat_id, "message_id": message_id},
        )
        return True
