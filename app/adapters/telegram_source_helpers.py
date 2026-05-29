"""Shared Telegram payload → domain-model helpers.

These functions operate on duck-typed Telegram message objects (Telethon events,
aiogram messages, or plain mocks) and produce domain-layer values.  They live
here — outside both ``app.adapters.content`` and ``app.adapters.telegram`` — so
that both packages can import them without violating the
``content-telegram-independence`` layering contract.
"""

from __future__ import annotations

from typing import Any

from app.domain.models.source import SourceItem, SourceKind


def coerce_telegram_messages(payload: Any) -> list[Any]:
    """Normalize a Telegram payload into an ordered list of message objects."""

    if payload is None:
        msg = "Telegram payload cannot be empty"
        raise ValueError(msg)
    if isinstance(payload, list | tuple):
        messages = [message for message in payload if message is not None]
    else:
        messages = [payload]
    if not messages:
        msg = "Telegram payload cannot be empty"
        raise ValueError(msg)
    return sorted(messages, key=_message_sort_key)


def classify_telegram_messages_source_kind(payload: Any) -> SourceKind:
    """Classify one Telegram message or an album payload."""

    messages = coerce_telegram_messages(payload)
    primary_message = messages[0]
    media_group_id = _coerce_str(getattr(primary_message, "media_group_id", None))
    has_media = any(_has_supported_media(message) for message in messages)
    if media_group_id and has_media:
        return SourceKind.TELEGRAM_ALBUM
    if has_media:
        return SourceKind.TELEGRAM_POST_WITH_IMAGES
    return SourceKind.TELEGRAM_POST


def build_source_item_from_telegram_payload(
    payload: Any,
    *,
    metadata: dict[str, Any] | None = None,
) -> SourceItem:
    """Build a stable source item for a Telegram message or album."""

    messages = coerce_telegram_messages(payload)
    primary_message = messages[0]
    message_ids = [_coerce_int(_message_id(message)) for message in messages]
    source_metadata = dict(metadata or {})
    source_metadata.setdefault(
        "message_ids",
        [message_id for message_id in message_ids if message_id is not None],
    )
    source_metadata.setdefault("media_count", _count_supported_media(messages))
    source_metadata.setdefault(
        "video_processing_strategy",
        "shared_video_source_extractor"
        if any(getattr(message, "video", None) for message in messages)
        else None,
    )
    return SourceItem.create(
        kind=classify_telegram_messages_source_kind(messages),
        original_value=_combine_telegram_text(messages) or "",
        telegram_chat_id=_coerce_int(getattr(getattr(primary_message, "chat", None), "id", None)),
        telegram_message_id=_coerce_int(_message_id(primary_message)),
        telegram_media_group_id=_coerce_str(getattr(primary_message, "media_group_id", None)),
        title_hint=_extract_telegram_title_hint(messages),
        metadata={k: v for k, v in source_metadata.items() if v is not None},
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _has_supported_media(message: Any) -> bool:
    return bool(
        getattr(message, "photo", None)
        or getattr(message, "document", None)
        or getattr(message, "video", None)
        or getattr(message, "animation", None)
    )


def _count_supported_media(messages: list[Any]) -> int:
    """Count media assets across all messages (photo, document, video, animation)."""
    count = 0
    for message in messages:
        for field in ("photo", "document", "video", "animation"):
            item = getattr(message, field, None)
            if item is None:
                continue
            if field == "photo" and isinstance(item, list):
                if item:
                    count += 1
            else:
                count += 1
    return count


def _combine_telegram_text(messages: list[Any]) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    for message in messages:
        text = _coerce_str(getattr(message, "text", None) or getattr(message, "caption", None))
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    if not parts:
        return None
    return "\n\n".join(parts)


def _extract_telegram_title_hint(messages: list[Any]) -> str | None:
    primary_message = messages[0]
    fwd_chat = getattr(primary_message, "forward_from_chat", None)
    if fwd_chat is not None:
        return _coerce_str(getattr(fwd_chat, "title", None))
    fwd_user = getattr(primary_message, "forward_from", None)
    if fwd_user is not None:
        return _build_forward_user_name(fwd_user)
    return _coerce_str(getattr(primary_message, "forward_sender_name", None))


def _build_forward_user_name(forward_from_user: Any) -> str | None:
    if forward_from_user is None:
        return None
    first_name = _coerce_str(getattr(forward_from_user, "first_name", None)) or ""
    last_name = _coerce_str(getattr(forward_from_user, "last_name", None)) or ""
    full_name = f"{first_name} {last_name}".strip()
    return full_name or None


def _message_sort_key(message: Any) -> tuple[int, str]:
    message_id = _coerce_int(_message_id(message))
    return (message_id or 0, str(getattr(message, "media_group_id", "") or ""))


def _message_id(message: Any) -> Any:
    return getattr(message, "id", getattr(message, "message_id", None))


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "build_source_item_from_telegram_payload",
    "classify_telegram_messages_source_kind",
    "coerce_telegram_messages",
]
