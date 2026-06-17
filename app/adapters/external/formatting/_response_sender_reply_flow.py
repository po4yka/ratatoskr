"""Reply-oriented flows for response sender."""

from __future__ import annotations

from typing import Any, cast

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.utils.retry_utils import retry_telegram_operation

from ._response_sender_shared import (
    ResponseSenderSharedState,
    build_message_kwargs,
    extract_message_id,
    validate_and_truncate,
)

logger = get_logger(__name__)


class ResponseSenderReplyFlow:
    """Handle safe reply operations, including reply-with-id."""

    def __init__(self, state: ResponseSenderSharedState) -> None:
        self._state = state

    async def safe_reply(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
        silent: bool = False,
    ) -> None:
        prepared = validate_and_truncate(
            self._state,
            text,
            substitute_on_unsafe=True,
            context_log_key="safe_reply",
        )
        if prepared is None:
            return
        text = prepared

        if not await self._state.validator.check_rate_limit():
            logger.debug("safe_reply_rate_limited", extra={"text_length": len(text)})

        if self._state.safe_reply_func is not None:
            kwargs: dict[str, Any] = {}
            if parse_mode is not None:
                kwargs["parse_mode"] = parse_mode
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            if silent:
                kwargs["silent"] = True
            await self._state.safe_reply_func(message, text, **kwargs)
            return

        try:
            msg_any: Any = message

            async def send() -> Any:
                kwargs = build_message_kwargs(
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                    silent=silent,
                )
                if message_thread_id is not None and self._state.telegram_client is not None:
                    client = getattr(self._state.telegram_client, "client", None)
                    chat_id = getattr(getattr(msg_any, "chat", None), "id", None)
                    if client is not None and chat_id is not None:
                        kwargs["message_thread_id"] = message_thread_id
                        return await client.send_message(chat_id, text, **kwargs)
                return await msg_any.reply_text(text, **kwargs)

            _, success = await retry_telegram_operation(send, operation_name="safe_reply")
            if success:
                logger.debug(
                    "reply_text_sent",
                    extra={"length": len(text), "has_buttons": reply_markup is not None},
                )
            else:
                logger.warning("safe_reply_retry_failed", extra={"text_length": len(text)})
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.error(
                "reply_failed",
                exc_info=True,
                extra={"error": str(exc), "text_length": len(text)},
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
        prepared_text = validate_and_truncate(
            self._state,
            text,
            substitute_on_unsafe=True,
            context_log_key="safe_reply_with_id",
        )
        if prepared_text is None:
            return None
        text = prepared_text

        if not await self._state.validator.check_rate_limit():
            logger.debug("safe_reply_with_id_rate_limited", extra={"text_length": len(text)})

        if self._state.safe_reply_func is not None:
            try:
                client = getattr(self._state.telegram_client, "client", None)
                chat = getattr(message, "chat", None)
                chat_id = getattr(chat, "id", None) if chat is not None else None
                if client is not None and chat_id is not None and hasattr(client, "send_message"):
                    logger.debug(
                        "reply_with_client_for_id",
                        extra={
                            "text_length": len(text),
                            "has_parse_mode": parse_mode is not None,
                            "chat_id": chat_id,
                        },
                    )
                    message_id = await self._send_via_client_with_id(
                        client,
                        chat_id,
                        text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                        message_thread_id=message_thread_id,
                    )
                    if message_id is not None:
                        return message_id
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.warning(
                    "reply_with_client_failed_fallback_custom", extra={"error": str(exc)}
                )

            logger.debug(
                "reply_with_custom_function",
                extra={"text_length": len(text), "has_parse_mode": parse_mode is not None},
            )
            try:
                await self._invoke_safe_reply_callback(
                    message,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                )
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.error(
                    "reply_failed",
                    exc_info=True,
                    extra={"error": str(exc), "text_length": len(text)},
                )
                return None
            logger.warning("reply_with_id_no_message_id", extra={"reason": "custom_reply_function"})
            return None

        try:
            if message_thread_id is not None:
                client = getattr(self._state.telegram_client, "client", None)
                chat = getattr(message, "chat", None)
                chat_id = getattr(chat, "id", None) if chat is not None else None
                if client is not None and chat_id is not None and hasattr(client, "send_message"):
                    return await self._send_via_client_with_id(
                        client,
                        chat_id,
                        text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview,
                        message_thread_id=message_thread_id,
                    )
            return await self._reply_text_with_id(
                message,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.error(
                "reply_failed",
                exc_info=True,
                extra={"error": str(exc), "text_length": len(text)},
            )
            return None

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Send a non-critical chat action to Telegram."""
        if not isinstance(chat_id, int) or chat_id == 0:
            logger.debug("send_chat_action_invalid_chat_id", extra={"chat_id": chat_id})
            return False

        client = None
        if self._state.telegram_client and hasattr(self._state.telegram_client, "client"):
            client = self._state.telegram_client.client
        if client is None or not hasattr(client, "send_chat_action"):
            logger.debug("chat_action_no_telegram_client", extra={"chat_id": chat_id})
            return False

        try:
            await client.send_chat_action(chat_id=chat_id, action=action)
            logger.debug("chat_action_sent", extra={"chat_id": chat_id, "action": action})
            return True
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug(
                "chat_action_send_failed",
                extra={"chat_id": chat_id, "action": action, "error": str(exc)},
            )
            return False

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        """React to a message with an emoji (non-critical status ack)."""
        if not isinstance(chat_id, int) or chat_id == 0 or not message_id:
            return False

        client = None
        if self._state.telegram_client and hasattr(self._state.telegram_client, "client"):
            client = self._state.telegram_client.client
        if client is None or not hasattr(client, "react"):
            return False

        try:
            await client.react(chat_id=chat_id, message_id=message_id, emoji=emoji)
            return True
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug("react_failed", extra={"chat_id": chat_id, "error": str(exc)})
            return False

    async def _send_via_client_with_id(
        self,
        client: Any,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        async def send() -> Any:
            kwargs = {"chat_id": chat_id, "text": text}
            kwargs.update(
                build_message_kwargs(
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                )
            )
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            return await client.send_message(**kwargs)

        sent, success = await retry_telegram_operation(send, operation_name="send_message_with_id")
        if not success or sent is None:
            logger.warning(
                "reply_with_id_retry_failed",
                extra={"chat_id": chat_id, "text_length": len(text)},
            )
            return None

        message_id = extract_message_id(sent)
        logger.debug(
            "reply_with_id_result",
            extra={"message_id": message_id, "sent_message_type": type(sent).__name__},
        )
        return message_id

    async def _invoke_safe_reply_callback(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> None:
        if self._state.safe_reply_func is None:
            return
        kwargs = build_message_kwargs(
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
        try:
            await self._state.safe_reply_func(message, text, **kwargs)
        except TypeError:
            kwargs.pop("reply_markup", None)
            kwargs.pop("disable_web_page_preview", None)
            await self._state.safe_reply_func(message, text, **kwargs)

    async def _reply_text_with_id(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> int | None:
        msg_any: Any = message

        async def reply() -> Any:
            kwargs = build_message_kwargs(
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
            return await msg_any.reply_text(text, **kwargs)

        sent_message, success = await retry_telegram_operation(
            reply, operation_name="reply_text_with_id"
        )
        if not success or sent_message is None:
            logger.warning("reply_text_retry_failed", extra={"text_length": len(text)})
            return None

        logger.debug("reply_text_sent", extra={"length": len(text)})
        message_id = getattr(sent_message, "message_id", None)
        logger.debug(
            "reply_with_id_result",
            extra={"message_id": message_id, "sent_message_type": type(sent_message).__name__},
        )
        return cast("int | None", message_id)
