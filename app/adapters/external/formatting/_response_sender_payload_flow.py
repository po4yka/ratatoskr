"""Payload and admin-log helpers for response sender."""

from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime
from typing import Any

from app.adapters.telegram.telethon_compat import InlineKeyboardButton, InlineKeyboardMarkup
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC

from ._response_sender_shared import ResponseSenderSharedState, build_json_filename

logger = get_logger(__name__)


def _build_success_envelope(
    data: dict[str, Any],
    *,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build a minimal success envelope matching the Mobile API wire shape.

    Mirrors ``app.api.models.responses.common.success_response`` for callers
    that live outside the API layer.  The shape is intentionally identical so
    clients cannot distinguish the source of the envelope.
    """
    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    meta: dict[str, Any] = {
        "correlation_id": correlation_id or "",
        "timestamp": ts,
        "version": os.getenv("APP_VERSION", "1.0.0"),
        "api_version": "1.0.0",
        "build": os.getenv("APP_BUILD") or None,
        "pagination": None,
        "debug": None,
    }
    return {"success": True, "data": data, "meta": meta}


class ResponseSenderPayloadFlow:
    """Handle JSON replies, inline keyboards, and admin logging."""

    def __init__(self, state: ResponseSenderSharedState, *, safe_reply: Any) -> None:
        self._state = state
        self._safe_reply = safe_reply

    async def reply_json(
        self,
        message: Any,
        obj: dict[str, Any],
        *,
        correlation_id: str | None = None,
        success: bool = True,
    ) -> None:
        if success and isinstance(obj, dict) and obj.get("success") in (True, False):
            payload = obj
        elif success:
            payload = _build_success_envelope(obj, correlation_id=correlation_id)
        else:
            payload = obj

        if self._state.reply_json_func is not None:
            await self._state.reply_json_func(message, payload)
            return

        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            with contextlib.closing(io.BytesIO(pretty.encode("utf-8"))) as bio:
                bio.name = build_json_filename(obj)
                msg_any: Any = message
                await msg_any.reply_document(bio, caption="📊 Full Summary JSON attached")
            return
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.error("reply_document_failed", extra={"error": str(exc)})

        await self._safe_reply(message, f"```json\n{pretty}\n```")

    @staticmethod
    def create_inline_keyboard(buttons: list[dict[str, str]]) -> Any:
        try:
            keyboard_buttons = [
                [InlineKeyboardButton(btn["text"], callback_data=btn["callback_data"])]
                for btn in buttons
            ]
            return InlineKeyboardMarkup(keyboard_buttons)
        except Exception as exc:
            logger.error("failed_to_create_inline_keyboard", extra={"error": str(exc)})
            return None

    async def send_to_admin_log(self, text: str, *, correlation_id: str | None = None) -> None:
        if self._state.admin_log_chat_id is None:
            return
        try:
            if correlation_id:
                text = f"[{correlation_id}] {text}"
            client = getattr(self._state.telegram_client, "client", None)
            if client is not None and hasattr(client, "send_message"):
                await client.send_message(chat_id=self._state.admin_log_chat_id, text=text[:4096])
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "admin_log_send_failed", extra={"chat_id": self._state.admin_log_chat_id}
            )
