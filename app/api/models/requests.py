"""Pydantic models for API request validation."""

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.core.url_utils import validate_url_input


class SubmitURLRequest(BaseModel):
    """Request body for submitting a URL."""

    type: Literal["url"] = "url"
    input_url: HttpUrl = Field(..., max_length=2048)
    lang_preference: Literal["auto", "en", "ru"] = "auto"

    @field_validator("input_url")
    @classmethod
    def validate_url(cls, v: HttpUrl) -> HttpUrl:
        """Validate URL scheme."""
        url_str = str(v)
        if not url_str.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        validate_url_input(url_str)
        return v


class ForwardMetadata(BaseModel):
    """Metadata for forwarded message."""

    from_chat_id: int = Field(..., ge=1)
    from_message_id: int = Field(..., ge=1)
    from_chat_title: str | None = None
    forwarded_at: str | None = None


class SubmitForwardRequest(BaseModel):
    """Request body for submitting a forwarded message."""

    type: Literal["forward"] = "forward"
    content_text: str = Field(min_length=10, max_length=100000)
    forward_metadata: ForwardMetadata
    lang_preference: Literal["auto", "en", "ru"] = "auto"


class UpdateSummaryRequest(BaseModel):
    """Request body for updating a summary."""

    is_read: bool | None = None


class UpdatePreferencesRequest(BaseModel):
    """Request body for updating user preferences."""

    lang_preference: Literal["auto", "en", "ru"] | None = None
    notification_settings: dict[str, Any] | None = None
    app_settings: dict[str, Any] | None = None


class SyncSessionRequest(BaseModel):
    """Session creation options."""

    limit: int | None = Field(default=None, ge=1, le=500)


class SyncApplyItem(BaseModel):
    """Single change to upload during sync."""

    entity_type: Literal["summary", "request", "preference", "stat", "crawl_result", "llm_call"]
    id: int | str = Field(description="Server-side identifier for the entity")
    action: Literal["update", "delete"]
    last_seen_version: int = Field(ge=0)
    payload: dict[str, Any] | None = None
    client_timestamp: str | None = Field(default=None, description="Client-side ISO timestamp")


class SyncApplyRequest(BaseModel):
    """Request body for applying local changes."""

    session_id: str
    changes: list[SyncApplyItem] = Field(min_length=1, max_length=500)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "Optional client-generated key (UUID is fine) for safe retries. "
            "When set, a duplicate apply with the same (session_id, "
            "idempotency_key) within ~5 minutes returns the original "
            "response without re-applying any changes. Lets a client retry "
            "after a network failure without risking double-apply."
        ),
    )


class CollectionCreateRequest(BaseModel):
    """Request body for creating a collection."""

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    parent_id: int | None = Field(default=None, ge=1)
    position: int | None = Field(default=None, ge=1)
    collection_type: str = Field(default="manual", pattern="^(manual|smart)$")
    query_conditions: list[dict[str, Any]] | None = None
    query_match_mode: str = Field(default="all", pattern="^(all|any)$")


class CollectionUpdateRequest(BaseModel):
    """Request body for updating a collection."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    parent_id: int | None = Field(default=None, ge=1)
    position: int | None = Field(default=None, ge=1)
    query_conditions: list[dict[str, Any]] | None = None
    query_match_mode: str | None = None


class CollectionItemCreateRequest(BaseModel):
    """Request body for adding an item to a collection."""

    summary_id: int


class CollectionReorderRequest(BaseModel):
    """Reorder child collections."""

    items: list[dict[str, int]] = Field(min_length=1)


class CollectionItemReorderRequest(BaseModel):
    """Reorder items inside a collection."""

    items: list[dict[str, int]] = Field(min_length=1)


class CollectionMoveRequest(BaseModel):
    """Move collection to a new parent."""

    parent_id: int | None = Field(default=None, ge=1)
    position: int | None = Field(default=None, ge=1)


class CollectionItemMoveRequest(BaseModel):
    """Move items to another collection."""

    summary_ids: list[int] = Field(min_length=1)
    target_collection_id: int
    position: int | None = Field(default=None, ge=1)


class CollectionShareRequest(BaseModel):
    """Add collaborator."""

    user_id: int
    role: Literal["editor", "viewer"]


class CollectionInviteRequest(BaseModel):
    """Create invite token."""

    role: Literal["editor", "viewer"]
    expires_at: str | None = None


class SubmitFeedbackRequest(BaseModel):
    """Request body for submitting summary feedback."""

    rating: int | None = None
    issues: list[str] | None = None
    comment: str | None = None


class CreateCustomDigestRequest(BaseModel):
    """Request body for creating a custom digest."""

    summary_ids: list[str] = Field(min_length=1)
    format: str = "markdown"
    title: str | None = None


class CreateHighlightRequest(BaseModel):
    """Request body for creating a highlight on a summary."""

    text: str = Field(min_length=1)
    start_offset: int | None = None
    end_offset: int | None = None
    color: str | None = None
    note: str | None = None


class UpdateHighlightRequest(BaseModel):
    """Request body for updating a highlight (color/note only)."""

    color: str | None = None
    note: str | None = None


class CreateGoalRequest(BaseModel):
    """Request body for creating or upserting a reading goal."""

    goal_type: Literal["daily", "weekly", "monthly"]
    target_count: int = Field(ge=1, le=1000)
    scope_type: Literal["global", "tag", "collection"] = "global"
    scope_id: int | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "CreateGoalRequest":
        if self.scope_type != "global":
            if self.scope_id is None or self.scope_id < 1:
                raise ValueError(
                    "scope_id must be a positive integer when scope_type is not 'global'"
                )
        elif self.scope_id is not None:
            raise ValueError("scope_id must be None when scope_type is 'global'")
        return self


class SaveReadingPositionRequest(BaseModel):
    """Save reading progress for a summary."""

    progress: float = Field(..., ge=0.0, le=1.0, description="Scroll progress 0.0-1.0")
    last_read_offset: int = Field(default=0, ge=0, description="Pixel or character offset")


class CreateTagRequest(BaseModel):
    """Request body for creating a tag."""

    name: str
    color: str | None = None


class UpdateTagRequest(BaseModel):
    """Request body for updating a tag."""

    name: str | None = None
    color: str | None = None


class MergeTagsRequest(BaseModel):
    """Request body for merging tags."""

    source_tag_ids: list[int]
    target_tag_id: int


class AttachTagsRequest(BaseModel):
    """Request body for attaching tags to a summary."""

    tag_ids: list[int] | None = None
    tag_names: list[str] | None = None


class CreateWebhookRequest(BaseModel):
    """Request body for creating a webhook subscription."""

    name: str | None = None
    url: str
    events: list[str]


class UpdateWebhookRequest(BaseModel):
    """Request body for updating a webhook subscription."""

    name: str | None = None
    url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


class AggregationBundleItemRequest(BaseModel):
    """One source item submitted to the aggregation API."""

    type: Literal["url"] = "url"
    url: HttpUrl = Field(..., max_length=2048)
    source_kind_hint: (
        Literal[
            "x_post",
            "x_article",
            "threads_post",
            "instagram_post",
            "instagram_carousel",
            "instagram_reel",
            "web_article",
            "telegram_post",
            "youtube_video",
        ]
        | None
    ) = None
    metadata: dict[str, Any] | None = None

    @field_validator("url")
    @classmethod
    def validate_item_url(cls, value: HttpUrl) -> HttpUrl:
        url_str = str(value)
        if not url_str.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        validate_url_input(url_str)
        return value

    @field_validator("metadata")
    @classmethod
    def validate_item_metadata_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        payload_size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        if payload_size > 2048:
            raise ValueError("Item metadata must be 2048 bytes or smaller")
        return value


class CreateAggregationBundleRequest(BaseModel):
    """Request body for bundle aggregation outside Telegram."""

    items: list[AggregationBundleItemRequest] = Field(min_length=1, max_length=25)
    lang_preference: Literal["auto", "en", "ru"] = "auto"
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def validate_bundle_metadata_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        payload_size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        if payload_size > 4096:
            raise ValueError("Bundle metadata must be 4096 bytes or smaller")
        return value


class CreateRuleRequest(BaseModel):
    """Request body for creating an automation rule."""

    name: str = Field(min_length=1, max_length=200)
    event_type: str
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(min_length=1)
    match_mode: str = "all"
    priority: int = Field(default=0, ge=0, le=1000)
    description: str | None = Field(default=None, max_length=500)


class UpdateRuleRequest(BaseModel):
    """Request body for updating an automation rule."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    event_type: str | None = None
    conditions: list[dict[str, Any]] | None = None
    actions: list[dict[str, Any]] | None = None
    match_mode: str | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class TestRuleRequest(BaseModel):
    """Request body for dry-run testing a rule."""

    __test__ = False

    summary_id: int


class ImportOptionsRequest(BaseModel):
    """Options for bookmark import."""

    summarize: bool = False
    create_tags: bool = True
    target_collection_id: int | None = None
    skip_duplicates: bool = True


class QuickSaveRequest(BaseModel):
    """Request body for browser extension quick-save."""

    url: str = Field(..., max_length=2048)
    title: str | None = None
    selected_text: str | None = None
    tag_names: list[str] = Field(default_factory=list)
    summarize: bool = True

    @field_validator("url")
    @classmethod
    def validate_quick_save_url(cls, value: str) -> str:
        validate_url_input(value)
        return value


# ---------------------------------------------------------------------------
# Repository endpoints
# ---------------------------------------------------------------------------


class IngestRepositoryRequest(BaseModel):
    """Request body for ingesting a GitHub repository by URL."""

    url: str = Field(..., min_length=10, max_length=500)


class RepositoryListSort(StrEnum):
    """Sort order for repository list."""

    STARS_DESC = "stars_desc"
    PUSHED_DESC = "pushed_desc"
    CREATED_DESC = "created_desc"
    FULL_NAME_ASC = "full_name_asc"
