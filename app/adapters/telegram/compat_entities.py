"""TL entity-translation helpers for Telethon → aiogram-style shapes.

Covers:
- ``_TELETHON_ENTITY_TYPES`` — class-name → MessageEntityType string map
- ``_translate_entities``    — raw Telethon entity list → _Entity list
- ``_build_typing_tl_action`` — aiogram action string → Telethon TL object
- ``_peer_to_id``            — Telethon peer → canonical marked int id
"""

from __future__ import annotations

from typing import Any

from app.adapters.telegram.compat_telethon import types, utils
from app.adapters.telegram.compat_types import _Entity

# Telethon encodes an entity's kind as the class name, not a ``.type`` field.
# Map the classes that correspond to ``MessageEntityType`` so the downstream
# parser (``_telegram_obj_to_dict`` -> ``MessageEntity.from_dict``) receives a
# real type instead of silently defaulting every entity to ``mention``.
_TELETHON_ENTITY_TYPES: dict[str, str] = {
    "MessageEntityMention": "mention",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "bot_command",
    "MessageEntityUrl": "url",
    "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone_number",
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityMentionName": "text_mention",
    "InputMessageEntityMentionName": "text_mention",
    "MessageEntityCustomEmoji": "custom_emoji",
}


def _translate_entities(raw: Any) -> list[_Entity]:
    """Translate a raw Telethon entity list to aiogram-shaped ``_Entity`` objects.

    Unrecognized entity classes (e.g. ``MessageEntityBlockquote``, which has no
    ``MessageEntityType`` counterpart) are skipped rather than mislabeled.
    """
    if not raw:
        return []
    translated: list[_Entity] = []
    for entity in raw:
        mapped = _TELETHON_ENTITY_TYPES.get(type(entity).__name__)
        if mapped is None:
            continue
        translated.append(
            _Entity(
                type=mapped,
                offset=int(getattr(entity, "offset", 0) or 0),
                length=int(getattr(entity, "length", 0) or 0),
                url=getattr(entity, "url", None),
            )
        )
    return translated


def _build_typing_tl_action(action: str | None) -> Any:
    """Map an aiogram-style chat-action string to a Telethon TL action object.

    Telegram's ``messages.setTyping`` takes a typed action; aiogram exposes it
    as a string ("typing", "upload_photo", ...). Unknown / future strings fall
    back to a generic typing action so the indicator still appears.
    Returns ``None`` only when Telethon itself is unavailable.
    """
    if types is None:  # pragma: no cover - telethon missing
        return None
    name = (action or "typing").strip().lower()
    # Upload-style actions take a ``progress`` field in modern Telethon.
    if name == "typing":
        return types.SendMessageTypingAction()
    if name == "cancel":
        return types.SendMessageCancelAction()
    if name == "upload_photo":
        return types.SendMessageUploadPhotoAction(progress=0)
    if name == "record_video":
        return types.SendMessageRecordVideoAction()
    if name == "upload_video":
        return types.SendMessageUploadVideoAction(progress=0)
    if name in {"record_voice", "record_audio"}:
        return types.SendMessageRecordAudioAction()
    if name in {"upload_voice", "upload_audio"}:
        return types.SendMessageUploadAudioAction(progress=0)
    if name == "upload_document":
        return types.SendMessageUploadDocumentAction(progress=0)
    if name == "find_location":
        return types.SendMessageGeoLocationAction()
    if name == "choose_contact":
        return types.SendMessageChooseContactAction()
    if name == "record_video_note":
        return types.SendMessageRecordRoundAction()
    if name == "upload_video_note":
        return types.SendMessageUploadRoundAction(progress=0)
    if name == "choose_sticker":
        return types.SendMessageChooseStickerAction()
    return types.SendMessageTypingAction()


def _peer_to_id(peer: Any) -> int | None:
    """Return the canonical marked id for a Telethon peer (-100… for channels).

    Returns None when the peer is missing or Telethon is unavailable.
    """
    if peer is None or utils is None:
        return None
    try:
        return int(utils.get_peer_id(peer))
    except (TypeError, ValueError, AttributeError):
        return None


__all__ = [
    "_TELETHON_ENTITY_TYPES",
    "_build_typing_tl_action",
    "_peer_to_id",
    "_translate_entities",
]
