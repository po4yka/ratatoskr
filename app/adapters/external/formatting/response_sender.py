"""Core Telegram message sending."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.telegram.draft_stream_sender import DraftStreamSender, DraftStreamSettings

from ._response_sender_draft_flow import ResponseSenderDraftFlow
from ._response_sender_edit_flow import ResponseSenderEditFlow
from ._response_sender_payload_flow import ResponseSenderPayloadFlow
from ._response_sender_reply_flow import ResponseSenderReplyFlow
from ._response_sender_shared import ResponseSenderSharedState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.external.formatting.protocols import MessageValidator


class ResponseSenderImpl:
    """Implementation of core Telegram message sending."""

    def __init__(
        self,
        validator: MessageValidator,
        *,
        max_message_chars: int = 3500,
        safe_reply_func: Callable[[Any, str], Awaitable[None]] | None = None,
        reply_json_func: Callable[[Any, dict[str, Any]], Awaitable[None]] | None = None,
        telegram_client: Any = None,
        admin_log_chat_id: int | None = None,
        draft_streaming_enabled: bool = True,
        draft_min_interval_ms: int = 700,
        draft_min_delta_chars: int = 40,
        draft_max_chars: int = 3500,
    ) -> None:
        self._state = ResponseSenderSharedState(
            validator=validator,
            max_message_chars=max_message_chars,
            safe_reply_func=safe_reply_func,
            reply_json_func=reply_json_func,
            telegram_client=telegram_client,
            admin_log_chat_id=admin_log_chat_id,
            draft_stream_sender=DraftStreamSender(
                telegram_client=telegram_client,
                settings=DraftStreamSettings(
                    enabled=draft_streaming_enabled,
                    min_interval_ms=draft_min_interval_ms,
                    min_delta_chars=draft_min_delta_chars,
                    max_chars=draft_max_chars,
                ),
            ),
        )
        self._reply_flow = ResponseSenderReplyFlow(self._state)
        self._edit_flow = ResponseSenderEditFlow(
            self._state,
            safe_reply_with_id=self._reply_flow.safe_reply_with_id,
        )
        self._draft_flow = ResponseSenderDraftFlow(
            self._state,
            edit_or_send=self._edit_flow.edit_or_send,
        )
        self._payload_flow = ResponseSenderPayloadFlow(
            self._state,
            safe_reply=self._reply_flow.safe_reply,
        )

    async def safe_reply(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        await self._reply_flow.safe_reply(
            message,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
        )

    async def safe_reply_with_id(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        return await self._reply_flow.safe_reply_with_id(
            message,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
        )

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
        return await self._edit_flow.edit_or_send(
            message,
            text,
            message_id=message_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
        )

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
        return await self._edit_flow.edit_message(
            chat_id,
            message_id,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        return await self._reply_flow.send_chat_action(chat_id, action=action)

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        return await self._reply_flow.react(chat_id, message_id, emoji)

    async def send_message_draft(
        self,
        message: Any,
        text: str,
        *,
        message_thread_id: int | None = None,
        force: bool = False,
    ) -> bool:
        return await self._draft_flow.send_message_draft(
            message,
            text,
            message_thread_id=message_thread_id,
            force=force,
        )

    def clear_message_draft(self, message: Any) -> None:
        self._draft_flow.clear_message_draft(message)

    def is_draft_streaming_enabled(self) -> bool:
        return self._draft_flow.is_draft_streaming_enabled()

    def set_telegram_client(self, telegram_client: Any) -> None:
        self._state.telegram_client = telegram_client
        self._draft_flow.set_telegram_client(telegram_client)

    def set_reply_callbacks(
        self,
        *,
        safe_reply_func: Any = ...,
        reply_json_func: Any = ...,
    ) -> None:
        if safe_reply_func is not ...:
            self._state.safe_reply_func = safe_reply_func
        if reply_json_func is not ...:
            self._state.reply_json_func = reply_json_func

    async def stream_or_edit_message(
        self,
        message: Any,
        text: str,
        *,
        message_id: int | None = None,
        parse_mode: str | None = "HTML",
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
        force_draft: bool = False,
    ) -> int | None:
        return await self._draft_flow.stream_or_edit_message(
            message,
            text,
            message_id=message_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
            force_draft=force_draft,
        )

    async def reply_json(
        self,
        message: Any,
        obj: dict[str, Any],
        *,
        correlation_id: str | None = None,
        success: bool = True,
    ) -> None:
        await self._payload_flow.reply_json(
            message,
            obj,
            correlation_id=correlation_id,
            success=success,
        )

    def create_inline_keyboard(self, buttons: list[dict[str, str]]) -> Any:
        return self._payload_flow.create_inline_keyboard(buttons)

    async def send_to_admin_log(self, text: str, *, correlation_id: str | None = None) -> None:
        await self._payload_flow.send_to_admin_log(text, correlation_id=correlation_id)
