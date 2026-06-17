"""Shared state and helpers for response-sender flows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from app.adapters.external.formatting.html_repair import repair_html_chunk
from app.adapters.telegram.telethon_compat import normalize_parse_mode as _normalize_parse_mode
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.external.formatting.protocols import MessageValidator
    from app.adapters.telegram.draft_stream_sender import DraftStreamSender

logger = get_logger(__name__)


@dataclass(slots=True)
class ResponseSenderSharedState:
    """Mutable runtime state shared by response-sender helper flows."""

    validator: MessageValidator
    max_message_chars: int
    safe_reply_func: Callable[[Any, str], Awaitable[None]] | None
    reply_json_func: Callable[[Any, dict[str, Any]], Awaitable[None]] | None
    telegram_client: Any
    admin_log_chat_id: int | None
    draft_stream_sender: DraftStreamSender


def normalize_parse_mode(mode: str | None) -> Any:
    """Normalize string parse modes for Telethon send methods."""
    return _normalize_parse_mode(mode)


def validate_and_truncate(
    state: ResponseSenderSharedState,
    text: str,
    *,
    substitute_on_unsafe: bool,
    context_log_key: str,
) -> str | None:
    """Validate text for safety and truncate if over the length limit."""
    if not text or not text.strip():
        logger.warning(f"{context_log_key}_empty_text")
        return None

    is_safe, error_msg = state.validator.validate_content(text)
    if not is_safe:
        logger.warning(
            f"{context_log_key}_unsafe_content_blocked",
            extra={"error": error_msg, "text_length": len(text)},
        )
        if substitute_on_unsafe:
            text = "❌ Message blocked for security reasons."
        else:
            return None

    if len(text) > state.max_message_chars:
        logger.warning(
            f"{context_log_key}_message_too_long",
            extra={"length": len(text), "max": state.max_message_chars},
        )
        text = text[: state.max_message_chars - 10] + "..."
        # Repair HTML tags that may have been cut by truncation.
        text = repair_html_chunk(text)
    return text


def build_message_kwargs(
    *,
    parse_mode: str | None = None,
    reply_markup: Any | None = None,
    disable_web_page_preview: bool | None = None,
    silent: bool = False,
) -> dict[str, Any]:
    """Build optional kwargs for Telegram send/edit methods."""
    kwargs: dict[str, Any] = {}
    if parse_mode is not None:
        kwargs["parse_mode"] = normalize_parse_mode(parse_mode)
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if disable_web_page_preview is not None:
        kwargs["disable_web_page_preview"] = disable_web_page_preview
    if silent:
        kwargs["silent"] = True
    return kwargs


def extract_message_id(sent_message: Any) -> int | None:
    """Extract a Telegram message ID from API response object."""
    message_id = getattr(sent_message, "message_id", None)
    if message_id is None:
        message_id = getattr(sent_message, "id", None)
    return cast("int | None", message_id)


def slugify(text: str, *, max_len: int = 60) -> str:
    """Create a filesystem-friendly slug from text."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\-\s]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "summary"


def build_json_filename(obj: dict[str, Any]) -> str:
    """Build a descriptive filename for a JSON attachment."""
    seo = obj.get("seo_keywords") or []
    base: str | None = None
    if isinstance(seo, list) and seo:
        base = "-".join(slugify(str(x)) for x in seo[:3] if str(x).strip())
    if not base:
        tl = str(obj.get("summary_250", "")).strip()
        if tl:
            words = re.findall(r"\w+", tl)[:6]
            base = slugify("-".join(words))
    if not base:
        base = "summary"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{base}-{timestamp}.json"
