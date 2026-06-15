"""Telegram Message model with comprehensive validation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.adapter_models.telegram.telegram_chat import TelegramChat
from app.adapter_models.telegram.telegram_entity import MessageEntity
from app.adapter_models.telegram.telegram_enums import MediaType, MessageEntityType
from app.adapter_models.telegram.telegram_user import TelegramUser
from app.core.logging_utils import get_logger, log_exception

logger = get_logger(__name__)


def _telegram_obj_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a Telegram client object to a dictionary, handling nested objects.

    This function recursively converts nested Telegram objects (like ChatPhoto,
    User, etc.) to dictionaries suitable for Pydantic model validation.

    Args:
        obj: A Telegram object or any object with __dict__ attribute

    Returns:
        A dictionary representation of the object with all nested objects converted
    """

    def _convert(value: Any, *, _seen: set[int], _depth: int) -> Any:
        if value is None:
            return None

        if isinstance(value, str | int | float | bool | datetime):
            return value

        if isinstance(value, Enum):
            return value.value

        if isinstance(value, dict):
            return {
                str(key): _convert(item, _seen=_seen, _depth=_depth + 1)
                for key, item in value.items()
            }

        if isinstance(value, list | tuple):
            return [_convert(item, _seen=_seen, _depth=_depth + 1) for item in value]

        if not hasattr(value, "__dict__"):
            # slots-based dataclasses (e.g. _Object) have __slots__ but no __dict__
            if hasattr(value, "__slots__"):
                slots_dict: dict[str, Any] = {}
                for key in value.__slots__:
                    if not str(key).startswith("_"):
                        slots_dict[str(key)] = _convert(
                            getattr(value, key, None), _seen=_seen, _depth=_depth + 1
                        )
                return slots_dict
            return value

        if _depth >= 8:
            return {}

        value_id = id(value)
        if value_id in _seen:
            return {}
        _seen.add(value_id)

        result: dict[str, Any] = {}
        for key, nested in value.__dict__.items():
            if isinstance(key, str) and key.startswith("_"):
                continue
            result[str(key)] = _convert(nested, _seen=_seen, _depth=_depth + 1)

        return result

    converted = _convert(obj, _seen=set(), _depth=0)
    return converted if isinstance(converted, dict) else {}


def _coerce_non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _parse_message_entity(entity: Any, *, label: str) -> MessageEntity:
    """Parse a MessageEntity while preserving a safe fallback."""
    entity_dict = _telegram_obj_to_dict(entity)
    try:
        return MessageEntity.from_dict(entity_dict)
    except Exception as exc:
        logger.warning("failed_to_parse_message_entity", extra={"label": label, "error": str(exc)})
        return MessageEntity(
            type=entity_dict.get("type", MessageEntityType.MENTION),
            offset=_coerce_non_negative_int(entity_dict.get("offset")),
            length=_coerce_non_negative_int(entity_dict.get("length")),
            url=entity_dict.get("url"),
            language=entity_dict.get("language"),
            custom_emoji_id=entity_dict.get("custom_emoji_id"),
        )


_MEDIA_FIELDS: tuple[str, ...] = (
    "photo",
    "video",
    "audio",
    "document",
    "sticker",
    "voice",
    "video_note",
    "animation",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
    "game",
    "invoice",
    "successful_payment",
    "story",
)

_MEDIA_TYPE_PRIORITY: tuple[tuple[str, MediaType], ...] = (
    ("photo", MediaType.PHOTO),
    ("video", MediaType.VIDEO),
    ("audio", MediaType.AUDIO),
    ("document", MediaType.DOCUMENT),
    ("sticker", MediaType.STICKER),
    ("voice", MediaType.VOICE),
    ("video_note", MediaType.VIDEO_NOTE),
    ("animation", MediaType.ANIMATION),
    ("contact", MediaType.CONTACT),
    ("location", MediaType.LOCATION),
    ("venue", MediaType.VENUE),
    ("poll", MediaType.POLL),
    ("dice", MediaType.DICE),
    ("game", MediaType.GAME),
    ("invoice", MediaType.INVOICE),
    ("successful_payment", MediaType.SUCCESSFUL_PAYMENT),
    ("story", MediaType.STORY),
)


def _parse_entity_collection(message: Any, *, attr: str, label: str) -> list[MessageEntity]:
    return [
        _parse_message_entity(entity, label=label) for entity in (getattr(message, attr, []) or [])
    ]


def _serialize_photo(photo: Any) -> list[dict[str, Any]] | None:
    if not photo:
        return None
    try:
        if isinstance(photo, list):
            return [photo_size.__dict__ for photo_size in photo]
        return [photo.__dict__]
    except (AttributeError, TypeError) as exc:
        log_exception(
            logger,
            "telegram_photo_parse_failed",
            exc,
            level="warning",
        )
        return None


def _extract_media_objects(message: Any) -> dict[str, Any]:
    return {name: getattr(message, name, None) for name in _MEDIA_FIELDS}


def _serialize_media_objects(media_objects: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"photo": _serialize_photo(media_objects.get("photo"))}
    for field_name in _MEDIA_FIELDS:
        if field_name == "photo":
            continue
        media_value = media_objects.get(field_name)
        payload[field_name] = media_value.__dict__ if media_value else None
    return payload


def _detect_media_type(media_objects: dict[str, Any]) -> MediaType | None:
    for field_name, media_type in _MEDIA_TYPE_PRIORITY:
        if media_objects.get(field_name):
            return media_type
    return None


def _parse_user(value: Any) -> TelegramUser | None:
    return TelegramUser.from_dict(_telegram_obj_to_dict(value)) if value else None


def _parse_chat(value: Any) -> TelegramChat | None:
    return TelegramChat.from_dict(_telegram_obj_to_dict(value)) if value else None


def _build_parsed_message_kwargs(message: Any) -> dict[str, Any]:
    text = getattr(message, "text", None)
    caption = getattr(message, "caption", None)
    entities = _parse_entity_collection(message, attr="entities", label="text")
    caption_entities = _parse_entity_collection(message, attr="caption_entities", label="caption")
    media_objects = _extract_media_objects(message)
    media_payload = _serialize_media_objects(media_objects)
    media_type = _detect_media_type(media_objects)

    forward_from = getattr(message, "forward_from", None)
    forward_from_chat = getattr(message, "forward_from_chat", None)
    forward_sender_name = getattr(message, "forward_sender_name", None)
    forward_date = getattr(message, "forward_date", None)
    reply_to_message = getattr(message, "reply_to_message", None)
    edit_date = getattr(message, "edit_date", None)
    reply_markup = getattr(message, "reply_markup", None)
    link_preview_options = getattr(message, "link_preview_options", None)

    return {
        "message_id": getattr(message, "id", 0),
        "from_user": _parse_user(getattr(message, "from_user", None)),
        "date": getattr(message, "date", None),
        "chat": _parse_chat(getattr(message, "chat", None)),
        "text": text,
        "entities": entities,
        "caption": caption,
        "caption_entities": caption_entities,
        "photo": media_payload["photo"],
        "video": media_payload["video"],
        "audio": media_payload["audio"],
        "document": media_payload["document"],
        "sticker": media_payload["sticker"],
        "voice": media_payload["voice"],
        "video_note": media_payload["video_note"],
        "animation": media_payload["animation"],
        "contact": media_payload["contact"],
        "location": media_payload["location"],
        "venue": media_payload["venue"],
        "poll": media_payload["poll"],
        "dice": media_payload["dice"],
        "game": media_payload["game"],
        "invoice": media_payload["invoice"],
        "successful_payment": media_payload["successful_payment"],
        "story": media_payload["story"],
        "forward_from": _parse_user(forward_from),
        "forward_from_chat": _parse_chat(forward_from_chat),
        "forward_from_message_id": getattr(message, "forward_from_message_id", None),
        "forward_signature": getattr(message, "forward_signature", None),
        "forward_sender_name": forward_sender_name,
        "forward_date": forward_date,
        "reply_to_message": reply_to_message.__dict__ if reply_to_message else None,
        "edit_date": edit_date,
        "media_group_id": getattr(message, "media_group_id", None),
        "author_signature": getattr(message, "author_signature", None),
        "via_bot": _parse_user(getattr(message, "via_bot", None)),
        "has_protected_content": getattr(message, "has_protected_content", None),
        "connected_website": getattr(message, "connected_website", None),
        "reply_markup": _telegram_obj_to_dict(reply_markup) if reply_markup is not None else None,
        "views": getattr(message, "views", None),
        "via_bot_user_id": getattr(message, "via_bot_user_id", None),
        "effect_id": getattr(message, "effect_id", None),
        "link_preview_options": (
            _telegram_obj_to_dict(link_preview_options)
            if link_preview_options is not None
            else None
        ),
        "show_caption_above_media": getattr(message, "show_caption_above_media", None),
        "media_type": media_type,
        "is_forwarded": bool(
            forward_from or forward_from_chat or forward_sender_name or forward_date
        ),
        "is_reply": bool(reply_to_message),
        "is_edited": bool(edit_date),
        "has_media": bool(media_type),
        "has_text": bool(text),
        "has_caption": bool(caption),
    }


def _extract_fallback_user(from_user_data: Any) -> TelegramUser | None:
    if not from_user_data:
        return None
    try:
        user_id = getattr(from_user_data, "id", 0)
        if user_id:
            return TelegramUser(
                id=int(user_id),
                is_bot=getattr(from_user_data, "is_bot", False),
                first_name=getattr(from_user_data, "first_name", "Unknown"),
                last_name=getattr(from_user_data, "last_name", None),
                username=getattr(from_user_data, "username", None),
                language_code=getattr(from_user_data, "language_code", None),
                is_premium=getattr(from_user_data, "is_premium", None),
                added_to_attachment_menu=getattr(from_user_data, "added_to_attachment_menu", None),
            )
    except Exception as user_exc:
        logger.warning(
            "Failed to extract user from failed message",
            extra={"error": str(user_exc)},
        )
        try:
            user_id = getattr(from_user_data, "id", 0)
            if user_id:
                return TelegramUser(
                    id=int(user_id),
                    is_bot=False,
                    first_name="Unknown",
                    last_name=None,
                    username=None,
                    language_code=None,
                    is_premium=None,
                    added_to_attachment_menu=None,
                )
        except Exception as fallback_user_error:
            logger.debug(
                "fallback_user_extraction_failed",
                extra={"error": str(fallback_user_error)},
            )
    return None


def _build_fallback_message_kwargs(message: Any) -> dict[str, Any]:
    media_objects = _extract_media_objects(message)
    media_payload = _serialize_media_objects(media_objects)
    photo_raw = media_objects.get("photo")
    return {
        "message_id": getattr(message, "id", 0),
        "from_user": _extract_fallback_user(getattr(message, "from_user", None)),
        "date": None,
        "chat": None,
        "text": getattr(message, "text", None),
        "caption": getattr(message, "caption", None),
        "photo": _serialize_photo(photo_raw),
        "photo_list": _serialize_photo(photo_raw),
        "video": media_payload["video"],
        "audio": media_payload["audio"],
        "document": media_payload["document"],
        "sticker": media_payload["sticker"],
        "voice": media_payload["voice"],
        "video_note": media_payload["video_note"],
        "animation": media_payload["animation"],
        "contact": media_payload["contact"],
        "location": media_payload["location"],
        "venue": media_payload["venue"],
        "poll": media_payload["poll"],
        "dice": media_payload["dice"],
        "game": media_payload["game"],
        "invoice": media_payload["invoice"],
        "successful_payment": media_payload["successful_payment"],
        "story": media_payload["story"],
    }


class TelegramMessage(BaseModel):
    """Comprehensive Telegram Message model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    message_id: int
    from_user: TelegramUser | None = None
    date: datetime | None = None
    chat: TelegramChat | None = None
    text: str | None = None
    entities: list[MessageEntity] = Field(default_factory=list)
    caption: str | None = None
    caption_entities: list[MessageEntity] = Field(default_factory=list)

    photo: list[dict[str, Any]] | None = None
    photo_list: list[dict[str, Any]] | None = None
    video: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    sticker: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    video_note: dict[str, Any] | None = None
    animation: dict[str, Any] | None = None
    contact: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    venue: dict[str, Any] | None = None
    poll: dict[str, Any] | None = None
    dice: dict[str, Any] | None = None
    game: dict[str, Any] | None = None
    invoice: dict[str, Any] | None = None
    successful_payment: dict[str, Any] | None = None
    story: dict[str, Any] | None = None

    forward_from: TelegramUser | None = None
    forward_from_chat: TelegramChat | None = None
    forward_from_message_id: int | None = None
    forward_signature: str | None = None
    forward_sender_name: str | None = None
    forward_date: datetime | None = None
    reply_to_message: dict[str, Any] | None = None

    edit_date: datetime | None = None
    media_group_id: str | None = None
    author_signature: str | None = None
    via_bot: TelegramUser | None = None
    has_protected_content: bool | None = None
    connected_website: str | None = None
    reply_markup: dict[str, Any] | None = None
    views: int | None = None
    via_bot_user_id: int | None = None
    effect_id: str | None = None
    link_preview_options: dict[str, Any] | None = None
    show_caption_above_media: bool | None = None

    media_type: MediaType | None = None
    is_forwarded: bool = False
    is_reply: bool = False
    is_edited: bool = False
    has_media: bool = False
    has_text: bool = False
    has_caption: bool = False

    @classmethod
    def from_telegram_message(cls, message: Any) -> TelegramMessage:
        """Create TelegramMessage from a Telegram client message object."""
        try:
            return cls(**_build_parsed_message_kwargs(message))
        except Exception as exc:
            logger.exception("Failed to parse Telegram message", extra={"error": str(exc)})
            return cls(**_build_fallback_message_kwargs(message))

    def get_effective_text(self) -> str | None:
        """Get the effective text content (text or caption)."""
        return self.text or self.caption

    def get_effective_entities(self) -> list[MessageEntity]:
        """Get the effective entities (text entities or caption entities)."""
        if self.text and self.entities:
            return self.entities
        if self.caption and self.caption_entities:
            return self.caption_entities
        return []

    def is_command(self) -> bool:
        """Check if message is a bot command."""
        text = self.get_effective_text()
        if not text:
            return False
        return text.startswith("/")

    def get_command(self) -> str | None:
        """Extract command from message text."""
        text = self.get_effective_text()
        if not text or not text.startswith("/"):
            return None

        parts = text.split()
        if parts:
            command = parts[0]
            if "@" in command:
                command = command.split("@")[0]
            return command
        return None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return self.model_dump(mode="json")

    def get_media_info(self) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Get media information based on media type."""
        if not self.media_type:
            return None

        media_map = {
            MediaType.PHOTO: self.photo,
            MediaType.VIDEO: self.video,
            MediaType.AUDIO: self.audio,
            MediaType.DOCUMENT: self.document,
            MediaType.STICKER: self.sticker,
            MediaType.VOICE: self.voice,
            MediaType.VIDEO_NOTE: self.video_note,
            MediaType.ANIMATION: self.animation,
            MediaType.CONTACT: self.contact,
            MediaType.LOCATION: self.location,
            MediaType.VENUE: self.venue,
            MediaType.POLL: self.poll,
            MediaType.DICE: self.dice,
            MediaType.GAME: self.game,
            MediaType.INVOICE: self.invoice,
            MediaType.SUCCESSFUL_PAYMENT: self.successful_payment,
            MediaType.STORY: self.story,
        }

        result = media_map.get(self.media_type)
        return result if isinstance(result, dict | list) else None

    def get_urls(self) -> list[str]:
        """Extract URLs from the effective text/caption and their entities.

        Covers both ``url`` entities (literal URL in the text) and ``text_link``
        entities (hyperlinked words, where the target lives in ``entity.url``).
        Uses ``get_effective_entities()`` so URLs in a media caption are not
        missed.
        """
        urls: list[str] = []

        text = self.get_effective_text()
        if not text:
            return urls

        for entity in self.get_effective_entities():
            if entity.type in [MessageEntityType.URL, MessageEntityType.TEXT_LINK]:
                if entity.type == MessageEntityType.URL:
                    url = text[entity.offset : entity.offset + entity.length]
                else:
                    url = entity.url or ""
                if url and url.strip():
                    urls.append(url.strip())

        return urls

    def validate_message(self) -> list[str]:
        """Validate message data and return list of validation errors."""
        errors = []

        if not isinstance(self.message_id, int) or self.message_id <= 0:
            errors.append("Message ID is required")

        if self.from_user:
            if not isinstance(self.from_user.id, int) or self.from_user.id <= 0:
                errors.append("Invalid from_user.id")
            if not self.from_user.first_name:
                errors.append("Missing from_user.first_name")

        if self.chat and not isinstance(self.chat.id, int):
            errors.append("Invalid chat.id")

        has_valid_content = self.text or self.caption or self.has_media or self.photo_list
        if not has_valid_content:
            errors.append("Message must have text, caption, or media content")

        text_len = len(self.text or "")
        for i, entity in enumerate(self.entities):
            offset = entity.offset if isinstance(entity.offset, int) else -1
            length = entity.length if isinstance(entity.length, int) else 0
            end = offset + length

            if offset < 0 or offset >= text_len:
                errors.append(f"Entity {i} offset out of range")
            if length <= 0:
                errors.append(f"Entity {i} length invalid")
            if end > text_len:
                errors.append("Entity extends beyond text length")

        caption_len = len(self.caption or "")
        for i, entity in enumerate(self.caption_entities):
            offset = entity.offset if isinstance(entity.offset, int) else -1
            length = entity.length if isinstance(entity.length, int) else 0
            end = offset + length

            if offset < 0 or offset >= caption_len:
                errors.append(f"Caption entity {i} offset out of range")
            if length <= 0:
                errors.append(f"Caption entity {i} length invalid")
            if end > caption_len:
                errors.append("Caption entity extends beyond text length")

        return errors


__all__ = ["TelegramMessage"]
