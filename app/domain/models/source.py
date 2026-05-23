"""Shared domain models for mixed-source aggregation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.url_utils import normalize_url

JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None
_STABLE_ID_PREFIX = "src_"
_URL_BACKED_KINDS = frozenset(
    {
        "x_post",
        "x_article",
        "threads_post",
        "instagram_post",
        "instagram_carousel",
        "instagram_reel",
        "web_article",
        "youtube_video",
        "academic_paper",
    }
)


class SourceKind(StrEnum):
    """Supported source kinds for bundle aggregation."""

    RSS = "rss"
    TELEGRAM_CHANNEL = "telegram_channel"
    X_POST = "x_post"
    X_ARTICLE = "x_article"
    THREADS_POST = "threads_post"
    INSTAGRAM_POST = "instagram_post"
    INSTAGRAM_CAROUSEL = "instagram_carousel"
    INSTAGRAM_REEL = "instagram_reel"
    GITHUB_REPOSITORY = "github_repository"
    ACADEMIC_PAPER = "academic_paper"
    WEB_ARTICLE = "web_article"
    TELEGRAM_POST = "telegram_post"
    TELEGRAM_POST_WITH_IMAGES = "telegram_post_with_images"
    TELEGRAM_ALBUM = "telegram_album"
    YOUTUBE_VIDEO = "youtube_video"
    FIELDTHEORY_BOOKMARK = "fieldtheory_bookmark"
    UNKNOWN = "unknown"


class AggregationSessionStatus(StrEnum):
    """Bundle-level lifecycle states."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AggregationItemStatus(StrEnum):
    """Per-item lifecycle states inside an aggregation session."""

    PENDING = "pending"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"


class UserSignalStatus(StrEnum):
    """Lifecycle states for proactive feed-item signals."""

    CANDIDATE = "candidate"
    QUEUED = "queued"
    DELIVERED = "delivered"
    DISMISSED = "dismissed"
    LIKED = "liked"
    ARCHIVED = "archived"


class SignalFilterStage(StrEnum):
    """Highest-cost stage reached by a signal candidate."""

    HEURISTIC = "heuristic"
    EMBEDDING = "embedding"
    LLM_JUDGE = "llm_judge"


@dataclass(slots=True, frozen=True)
class SignalSource:
    """Domain representation of a source that emits feed items."""

    id: int
    kind: SourceKind
    external_id: str | None = None
    url: str | None = None
    title: str | None = None
    is_active: bool = True


@dataclass(slots=True, frozen=True)
class SignalFeedItem:
    """Domain representation of an ingested item before scoring."""

    id: int
    source_id: int
    external_id: str
    canonical_url: str | None = None
    title: str | None = None
    content_text: str | None = None


@dataclass(slots=True, frozen=True)
class UserSignal:
    """Domain representation of a user's scored candidate item."""

    id: int
    user_id: int
    feed_item_id: int
    status: UserSignalStatus
    final_score: float | None = None
    filter_stage: SignalFilterStage = SignalFilterStage.HEURISTIC


@dataclass(slots=True, frozen=True)
class SourceItem:
    """Normalized representation of one source inside an aggregation bundle."""

    kind: SourceKind
    stable_id: str
    dedupe_key: str
    original_value: str | None = None
    normalized_value: str | None = None
    external_id: str | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    telegram_media_group_id: str | None = None
    request_id: int | None = None
    title_hint: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        kind: SourceKind,
        original_value: str | None = None,
        normalized_value: str | None = None,
        external_id: str | None = None,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
        telegram_media_group_id: str | None = None,
        request_id: int | None = None,
        title_hint: str | None = None,
        metadata: dict[str, JSONValue] | None = None,
    ) -> SourceItem:
        """Build a source item with deterministic identity and dedupe keys."""

        normalized_candidate = cls._normalize_value(
            kind=kind,
            original_value=original_value,
            normalized_value=normalized_value,
        )
        dedupe_key = cls._build_dedupe_key(
            kind=kind,
            original_value=original_value,
            normalized_value=normalized_candidate,
            external_id=external_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            telegram_media_group_id=telegram_media_group_id,
        )
        stable_id = f"{_STABLE_ID_PREFIX}{hashlib.sha256(dedupe_key.encode('utf-8')).hexdigest()}"
        return cls(
            kind=kind,
            stable_id=stable_id,
            dedupe_key=dedupe_key,
            original_value=cls._clean_text(original_value),
            normalized_value=normalized_candidate,
            external_id=cls._clean_text(external_id),
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            telegram_media_group_id=cls._clean_text(telegram_media_group_id),
            request_id=request_id,
            title_hint=cls._clean_text(title_hint),
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def _clean_text(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @classmethod
    def _normalize_value(
        cls,
        *,
        kind: SourceKind,
        original_value: str | None,
        normalized_value: str | None,
    ) -> str | None:
        if normalized_value and normalized_value.strip():
            return normalized_value.strip()
        if kind.value not in _URL_BACKED_KINDS:
            return None
        if not original_value or not original_value.strip():
            return None
        try:
            return normalize_url(original_value)
        except ValueError:
            return original_value.strip()

    @classmethod
    def _build_dedupe_key(
        cls,
        *,
        kind: SourceKind,
        original_value: str | None,
        normalized_value: str | None,
        external_id: str | None,
        telegram_chat_id: int | None,
        telegram_message_id: int | None,
        telegram_media_group_id: str | None,
    ) -> str:
        cleaned_external_id = cls._clean_text(external_id)
        cleaned_group_id = cls._clean_text(telegram_media_group_id)
        cleaned_original = cls._clean_text(original_value)

        if cleaned_external_id:
            return f"{kind.value}:external:{cleaned_external_id}"
        if telegram_chat_id is not None and cleaned_group_id:
            return f"{kind.value}:telegram_media_group:{telegram_chat_id}:{cleaned_group_id}"
        if telegram_chat_id is not None and telegram_message_id is not None:
            return f"{kind.value}:telegram_message:{telegram_chat_id}:{telegram_message_id}"
        if normalized_value:
            return f"{kind.value}:url:{normalized_value}"
        if cleaned_original:
            digest = hashlib.sha256(cleaned_original.encode("utf-8")).hexdigest()
            return f"{kind.value}:raw:{digest}"

        msg = f"Cannot derive a stable source identity for kind={kind.value}"
        raise ValueError(msg)

    def to_dict(self) -> dict[str, JSONValue]:
        """Return a JSON-safe representation for persistence."""

        return {
            "kind": self.kind.value,
            "stable_id": self.stable_id,
            "dedupe_key": self.dedupe_key,
            "original_value": self.original_value,
            "normalized_value": self.normalized_value,
            "external_id": self.external_id,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_message_id": self.telegram_message_id,
            "telegram_media_group_id": self.telegram_media_group_id,
            "request_id": self.request_id,
            "title_hint": self.title_hint,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True, frozen=True)
class SourceBundle:
    """Ordered collection of source items submitted together."""

    items: tuple[SourceItem, ...]

    def __post_init__(self) -> None:
        if not self.items:
            msg = "Source bundle must contain at least one source item"
            raise ValueError(msg)

    @classmethod
    def from_items(cls, items: list[SourceItem] | tuple[SourceItem, ...]) -> SourceBundle:
        return cls(items=tuple(items))

    def first_positions_by_source_id(self) -> dict[str, int]:
        """Return the first position for every stable source ID."""

        first_positions: dict[str, int] = {}
        for position, item in enumerate(self.items):
            first_positions.setdefault(item.stable_id, position)
        return first_positions

    def duplicate_positions(self) -> dict[int, int]:
        """Map duplicate positions to their first occurrence."""

        first_positions = self.first_positions_by_source_id()
        duplicates: dict[int, int] = {}
        for position, item in enumerate(self.items):
            first_position = first_positions[item.stable_id]
            if first_position != position:
                duplicates[position] = first_position
        return duplicates

    @property
    def unique_items(self) -> tuple[SourceItem, ...]:
        """Return bundle items with later duplicates removed."""

        first_positions = self.first_positions_by_source_id()
        return tuple(
            item
            for position, item in enumerate(self.items)
            if first_positions[item.stable_id] == position
        )


@dataclass(slots=True, frozen=True)
class AggregationRequest:
    """Application-facing request to aggregate a source bundle."""

    bundle: SourceBundle
    correlation_id: str | None = None
    user_id: int | None = None
    allow_partial_success: bool = True
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    @classmethod
    def from_items(
        cls,
        items: list[SourceItem] | tuple[SourceItem, ...],
        *,
        correlation_id: str | None = None,
        user_id: int | None = None,
        allow_partial_success: bool = True,
        metadata: dict[str, JSONValue] | None = None,
    ) -> AggregationRequest:
        return cls(
            bundle=SourceBundle.from_items(items),
            correlation_id=correlation_id.strip() if correlation_id else None,
            user_id=user_id,
            allow_partial_success=allow_partial_success,
            metadata=dict(metadata or {}),
        )

    @property
    def total_items(self) -> int:
        return len(self.bundle.items)


__all__ = [
    "AggregationItemStatus",
    "AggregationRequest",
    "AggregationSessionStatus",
    "SourceBundle",
    "SourceItem",
    "SourceKind",
]
