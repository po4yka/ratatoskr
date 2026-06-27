"""Core SQLAlchemy models for users, requests, summaries, and media records."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
import enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JSONB, TSVECTOR, JSONValue, _next_server_version, _utcnow


class LLMAttemptTrigger(enum.StrEnum):
    """Identifies which pathway created this LLM call row.

    Values:
    - ``initial``: first call for the request (the request just landed).
    - ``user_retry``: first call of a request cloned by
      ``RequestService.retry_failed_request`` (mobile API retry action).
    - ``auto_backfill``: reserved for an automated backfill / scheduled retry
      pipeline. No current code path writes this value.
    - ``repair_loop``: call issued by the JSON-repair self-correction path in
      ``app/adapters/content/llm_response_workflow_repair.py``.
    - ``stream_fallback_retry``: reserved for a fresh request created when
      streaming falls back to non-streaming. The current implementation reuses
      the same in-flight ``LLMCall`` row rather than inserting a new one, so
      this value is not written by any active code path.
    - ``webwright_tool``: re-summarization after the Webwright sidecar
      (microsoft/Webwright) was used to enrich thin/paywalled content.
      RESERVED — the ``WebwrightEnricher`` (Path C) that wrote this value
      has been removed; no active code path sets this trigger.
    - ``graph_node``: LLM call issued by a node of the LangGraph summarize
      graph (the graph orchestration path; ADR-0001/0011). RESERVED — added by
      the checkpoint-infrastructure track ahead of the graph cutover; no active
      code path writes this value yet (the graph runs behind a feature flag).
    """

    initial = "initial"
    user_retry = "user_retry"
    auto_backfill = "auto_backfill"
    repair_loop = "repair_loop"
    stream_fallback_retry = "stream_fallback_retry"
    webwright_tool = "webwright_tool"
    graph_node = "graph_node"


_llm_attempt_trigger_enum = Enum(
    LLMAttemptTrigger,
    name="llm_attempt_trigger",
    native_enum=True,
    create_constraint=True,
    values_callable=lambda obj: [e.value for e in obj],
)


def _json_column() -> Mapped[JSONValue]:
    return mapped_column(JSONB, nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_linked_telegram_user_id", "linked_telegram_user_id"),)

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    preferences_json: Mapped[JSONValue] = _json_column()
    onboarding_completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locale: Mapped[str] = mapped_column(Text, default="en", nullable=False)
    theme: Mapped[str] = mapped_column(Text, default="dark", nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_summary_language: Mapped[str] = mapped_column(Text, default="auto", nullable=False)
    linked_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    linked_telegram_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_telegram_photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_telegram_first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_telegram_last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    link_nonce: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_nonce_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    client_secrets: Mapped[list[ClientSecret]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    credentials: Mapped[UserCredential | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    identities: Mapped[list[Any]] = relationship(
        "UserIdentity", back_populates="user", cascade="all, delete-orphan"
    )
    magic_link_tokens: Mapped[list[Any]] = relationship(
        "MagicLinkToken", back_populates="user", cascade="all, delete-orphan"
    )
    devices: Mapped[list[UserDevice]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    aggregation_sessions: Mapped[list[Any]] = relationship(
        "AggregationSession", back_populates="user", cascade="all, delete-orphan"
    )
    batch_sessions: Mapped[list[Any]] = relationship(
        "BatchSession", back_populates="user", cascade="all, delete-orphan"
    )
    collections: Mapped[list[Any]] = relationship(
        "Collection", back_populates="user", cascade="all, delete-orphan"
    )
    collection_collaborations: Mapped[list[Any]] = relationship(
        "CollectionCollaborator",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="CollectionCollaborator.user_id",
    )
    collection_invites_sent: Mapped[list[Any]] = relationship(
        "CollectionCollaborator",
        back_populates="invited_by_user",
        foreign_keys="CollectionCollaborator.invited_by_id",
    )
    channel_categories: Mapped[list[Any]] = relationship(
        "ChannelCategory", back_populates="user", cascade="all, delete-orphan"
    )
    channel_subscriptions: Mapped[list[Any]] = relationship(
        "ChannelSubscription", back_populates="user", cascade="all, delete-orphan"
    )
    digest_deliveries: Mapped[list[Any]] = relationship(
        "DigestDelivery", back_populates="user", cascade="all, delete-orphan"
    )
    email_addresses: Mapped[list[Any]] = relationship(
        "UserEmailAddress", back_populates="user", cascade="all, delete-orphan"
    )
    email_deliveries: Mapped[list[Any]] = relationship(
        "EmailDelivery", back_populates="user", cascade="all, delete-orphan"
    )
    export_integrations: Mapped[list[Any]] = relationship(
        "UserExportIntegration", back_populates="user", cascade="all, delete-orphan"
    )
    digest_preferences: Mapped[Any | None] = relationship(
        "UserDigestPreference", back_populates="user", cascade="all, delete-orphan"
    )
    rss_subscriptions: Mapped[list[Any]] = relationship(
        "RSSFeedSubscription", back_populates="user", cascade="all, delete-orphan"
    )
    webhooks: Mapped[list[Any]] = relationship(
        "WebhookSubscription", back_populates="user", cascade="all, delete-orphan"
    )
    rules: Mapped[list[Any]] = relationship(
        "AutomationRule", back_populates="user", cascade="all, delete-orphan"
    )
    import_jobs: Mapped[list[Any]] = relationship(
        "ImportJob", back_populates="user", cascade="all, delete-orphan"
    )
    backups: Mapped[list[Any]] = relationship(
        "UserBackup", back_populates="user", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[list[Any]] = relationship(
        "Subscription", back_populates="user", cascade="all, delete-orphan"
    )
    signal_topics: Mapped[list[Any]] = relationship(
        "Topic", back_populates="user", cascade="all, delete-orphan"
    )
    saved_searches: Mapped[list[Any]] = relationship(
        "SavedSearch", back_populates="user", cascade="all, delete-orphan"
    )
    search_history_entries: Mapped[list[Any]] = relationship(
        "SearchHistoryEntry", back_populates="user", cascade="all, delete-orphan"
    )
    user_signals: Mapped[list[Any]] = relationship(
        "UserSignal", back_populates="user", cascade="all, delete-orphan"
    )
    summary_feedbacks: Mapped[list[Any]] = relationship(
        "SummaryFeedback", back_populates="user", cascade="all, delete-orphan"
    )
    custom_digests: Mapped[list[Any]] = relationship(
        "CustomDigest", back_populates="user", cascade="all, delete-orphan"
    )
    highlights: Mapped[list[Any]] = relationship(
        "SummaryHighlight", back_populates="user", cascade="all, delete-orphan"
    )
    goals: Mapped[list[Any]] = relationship(
        "UserGoal", back_populates="user", cascade="all, delete-orphan"
    )
    tags: Mapped[list[Any]] = relationship(
        "Tag", back_populates="user", cascade="all, delete-orphan"
    )


class ClientSecret(Base):
    __tablename__ = "client_secrets"
    __table_args__ = (
        Index("ix_client_secrets_user_id_client_id", "user_id", "client_id"),
        Index("ix_client_secrets_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    secret_salt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="active", nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="client_secrets")


class UserCredential(Base):
    """Owner-managed nickname/email + password credentials for web/JWT login.

    Independent of ClientSecret (machine-client secrets) -- argon2id-hashed
    user passwords with HMAC pre-hash (CREDENTIALS_LOGIN_PEPPER). One row per
    user (UNIQUE constraint on user_id) for the single-owner deployment;
    schema permits future multi-credential extension by relaxing the UNIQUE.
    """

    __tablename__ = "user_credentials"
    __table_args__ = (Index("ix_user_credentials_locked_until", "locked_until"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    nickname: Mapped[str] = mapped_column(Text, nullable=False)
    nickname_canonical: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_canonical: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    pepper_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="credentials")


class UserIdentity(Base):
    """External identity linked to an existing Ratatoskr user."""

    __tablename__ = "user_identities"
    __table_args__ = (
        Index("ux_user_identities_provider_subject", "provider", "subject", unique=True),
        Index("ix_user_identities_user_id", "user_id"),
        Index("ix_user_identities_email_canonical", "email_canonical"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_canonical: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="identities")


class MagicLinkToken(Base):
    """Hashed one-time passwordless login token."""

    __tablename__ = "magic_link_tokens"
    __table_args__ = (
        Index("ix_magic_link_tokens_token_hash", "token_hash", unique=True),
        Index("ix_magic_link_tokens_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="magic_link_tokens")


class Chat(Base):
    __tablename__ = "chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_user_id", "user_id"),
        Index("ix_requests_status", "status"),
        Index("ix_requests_created_at", "created_at"),
        Index("ix_requests_user_id_created_at", "user_id", "created_at"),
        # correlation_id is the cross-cutting trace key; index it for lookups.
        Index("ix_requests_correlation_id", "correlation_id"),
        Index(
            "ux_requests_user_dedupe_hash",
            "user_id",
            "dedupe_hash",
            unique=True,
            postgresql_where=text("dedupe_hash IS NOT NULL"),
        ),
        Index(
            "ux_requests_user_paper_canonical_id",
            "user_id",
            "paper_canonical_id",
            unique=True,
            postgresql_where=text("paper_canonical_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Canonical paper identifier (e.g. "arxiv:2301.00001", "ssrn:6531478",
    # "doi:10.xxxx/...") for academic-paper requests. Lets two different URLs
    # pointing at the same paper (/abs/X and /pdf/X.pdf, v1 and v2) dedupe to
    # one request per user via ux_requests_user_paper_canonical_id. Nullable for
    # every non-academic request.
    paper_canonical_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bot_reply_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fwd_from_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fwd_from_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lang_detected: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_timestamp: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_context_json: Mapped[JSONValue] = _json_column()
    # When set, the first LLM call for this request will inherit this trigger
    # value rather than defaulting to "initial".  Used by retry flows to mark
    # cloned requests as "user_retry" without modifying every LLM call site.
    initial_attempt_trigger: Mapped[str | None] = mapped_column(Text, nullable=True)

    telegram_message: Mapped[TelegramMessage | None] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    crawl_result: Mapped[CrawlResult | None] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    llm_calls: Mapped[list[LLMCall]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    summary: Mapped[Summary | None] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    interactions: Mapped[list[UserInteraction]] = relationship(back_populates="request")
    video_download: Mapped[VideoDownload | None] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    attachment: Mapped[AttachmentProcessing | None] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    aggregation_items: Mapped[list[Any]] = relationship(
        "AggregationSessionItem", back_populates="request"
    )
    batch_item: Mapped[Any | None] = relationship(
        "BatchSessionItem", back_populates="request", cascade="all, delete-orphan"
    )
    processing_job: Mapped[RequestProcessingJob | None] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    progress_events: Mapped[list[ProgressEvent]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class RequestProcessingJob(Base):
    __tablename__ = "request_processing_jobs"
    __table_args__ = (
        Index("ix_request_processing_jobs_status_retry", "status", "retry_after"),
        Index("ix_request_processing_jobs_lease_expires_at", "lease_expires_at"),
        Index("ix_request_processing_jobs_updated_at", "updated_at"),
        Index("ix_request_processing_jobs_correlation_id", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(Text, default="queued", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_after: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    request: Mapped[Request] = relationship(back_populates="processing_job")


class ProgressEvent(Base):
    __tablename__ = "progress_events"
    __table_args__ = (
        UniqueConstraint("request_id", "sequence", name="uq_progress_events_request_sequence"),
        Index("ix_progress_events_request_sequence", "request_id", "sequence"),
        Index("ix_progress_events_event_id", "event_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[JSONValue] = _json_column()
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    request: Mapped[Request] = relationship(back_populates="progress_events")


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    date_ts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_full: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities_json: Mapped[JSONValue] = _json_column()
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_ids_json: Mapped[JSONValue] = _json_column()
    forward_from_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    forward_from_chat_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    forward_from_chat_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    forward_from_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forward_date_ts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_raw_json: Mapped[JSONValue] = _json_column()

    request: Mapped[Request] = relationship(back_populates="telegram_message")


class CrawlResult(Base):
    __tablename__ = "crawl_results"
    __table_args__ = (Index("ix_crawl_results_correlation_id", "correlation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[JSONValue] = _json_column()
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_json: Mapped[JSONValue] = _json_column()
    metadata_json: Mapped[JSONValue] = _json_column()
    links_json: Mapped[JSONValue] = _json_column()
    screenshots_paths_json: Mapped[JSONValue] = _json_column()
    firecrawl_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    firecrawl_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    firecrawl_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    firecrawl_details_json: Mapped[JSONValue] = _json_column()
    raw_response_json: Mapped[JSONValue] = _json_column()
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scraper-chain telemetry populated from ScraperAttemptRecorder output.
    # See app/adapters/content/scraper/attempt_log.py.
    attempt_log: Mapped[JSONValue] = _json_column()
    winning_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    request: Mapped[Request] = relationship(back_populates="crawl_result")


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        # Unique constraint enforces the monotonic-per-request attempt_index
        # invariant documented in CLAUDE.md and prevents concurrent retry races.
        # Migration 0009 replaced the old non-unique index with this constraint.
        UniqueConstraint(
            "request_id",
            "attempt_index",
            name="uq_llm_calls_request_id_attempt_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )
    attempt_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="1-based index within a request's attempt sequence.",
    )
    attempt_trigger: Mapped[str] = mapped_column(
        _llm_attempt_trigger_enum,
        nullable=False,
        default=LLMAttemptTrigger.initial,
        server_default=LLMAttemptTrigger.initial.value,
        comment="Pathway that created this LLM call row. See LLMAttemptTrigger.",
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_headers_json: Mapped[JSONValue] = _json_column()
    request_messages_json: Mapped[JSONValue] = _json_column()
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[JSONValue] = _json_column()
    openrouter_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    openrouter_response_json: Mapped[JSONValue] = _json_column()
    tokens_prompt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_completion: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_output_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    structured_output_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_context_json: Mapped[JSONValue] = _json_column()
    # Retry-budget telemetry — populated by the OpenRouter chat
    # response handler when wiring lands. Migration 0014 adds the
    # backing DB columns. See docs/reference/llm-retry-telemetry.md.
    fallback_model_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_exhausted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    request: Mapped[Request] = relationship(back_populates="llm_calls")
    digest_analyses: Mapped[list[Any]] = relationship(
        "ChannelPostAnalysis", back_populates="llm_call"
    )


class Summary(Base):
    __tablename__ = "summaries"
    __table_args__ = (
        Index("ix_summaries_is_read", "is_read"),
        Index("ix_summaries_lang", "lang"),
        Index("ix_summaries_created_at", "created_at"),
        # Partial index for the reconciler's ORDER BY updated_at scan over
        # non-deleted rows. Added in migration 0010.
        Index(
            "ix_summaries_updated_at_where_not_deleted",
            "updated_at",
            postgresql_where="is_deleted = false",
        ),
        # Favorited summaries are a small subset; a partial index keeps the
        # favorites listing fast without indexing the (mostly-false) full column.
        Index(
            "ix_summaries_is_favorited",
            "is_favorited",
            postgresql_where="is_favorited = true",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    lang: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_payload: Mapped[JSONValue] = _json_column()
    insights_json: Mapped[JSONValue] = _json_column()
    # Full Russian bilingual rendering of json_payload (same SummaryModel shape),
    # written when SUMMARY_BILINGUAL_ENABLED produces a second-language summary.
    # NULL when absent; the primary json_payload/lang are never overwritten by it.
    ru_payload: Mapped[JSONValue] = _json_column()
    # Denormalized metadata columns (migration 0030 / audit findings 7A, 5C).
    # These mirror fields inside json_payload so list-view and smart-collection
    # scan queries can project scalar columns without loading the full JSONB blob.
    # The write path keeps them in sync via _extract_summary_metadata().
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    reading_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    topic_tags: Mapped[JSONValue] = _json_column()
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_favorited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reading_progress: Mapped[float | None] = mapped_column(Float, default=0.0, nullable=True)
    last_read_offset: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    request: Mapped[Request] = relationship(back_populates="summary")
    embedding: Mapped[SummaryEmbedding | None] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )
    audio_generations: Mapped[list[AudioGeneration]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )
    collection_items: Mapped[list[Any]] = relationship(
        "CollectionItem", back_populates="summary", cascade="all, delete-orphan"
    )
    feedbacks: Mapped[list[Any]] = relationship(
        "SummaryFeedback", back_populates="summary", cascade="all, delete-orphan"
    )
    highlights: Mapped[list[Any]] = relationship(
        "SummaryHighlight", back_populates="summary", cascade="all, delete-orphan"
    )
    summary_tags: Mapped[list[Any]] = relationship(
        "SummaryTag", back_populates="summary", cascade="all, delete-orphan"
    )
    rule_execution_logs: Mapped[list[Any]] = relationship(
        "RuleExecutionLog", back_populates="summary"
    )


class UserInteraction(Base):
    __tablename__ = "user_interactions"
    __table_args__ = (
        Index("ix_user_interactions_user_id", "user_id"),
        Index("ix_user_interactions_request_id", "request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interaction_type: Mapped[str] = mapped_column(Text, nullable=False)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_forward: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    forward_from_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    forward_from_chat_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    forward_from_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_output_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    response_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    response_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_occurred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    request: Mapped[Request | None] = relationship(back_populates="interactions")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_ts", "ts"),
        Index("ix_audit_logs_event", "event"),
        # GIN index backs the JSONB containment filter (details_json @> {...})
        # used by the admin audit-log user_id lookup.
        Index("ix_audit_logs_details_json", "details_json", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    level: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[JSONValue] = _json_column()


class SummaryEmbedding(Base):
    __tablename__ = "summary_embeddings"
    __table_args__ = (
        Index("ix_summary_embeddings_model_name_model_version", "model_name", "model_version"),
        # Covering composite index for the reconciler join probe on
        # (summary_id, last_indexed_at). Added in migration 0010.
        Index("ix_summary_embeddings_summary_id_last_indexed", "summary_id", "last_indexed_at"),
        # Reconciler counts/filters rows by index_status (e.g. != 'indexed').
        Index("ix_summary_embeddings_index_status", "index_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("summaries.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True, index=False)
    last_indexed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    index_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")

    summary: Mapped[Summary] = relationship(back_populates="embedding")


class VideoDownload(Base):
    __tablename__ = "video_downloads"
    __table_args__ = (
        Index("ix_video_downloads_video_id", "video_id"),
        Index("ix_video_downloads_status", "status"),
        Index("ix_video_downloads_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    video_id: Mapped[str] = mapped_column(Text, nullable=False)
    video_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtitle_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    like_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(Text, nullable=True)
    format_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtitle_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_generated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    download_completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    request: Mapped[Request] = relationship(back_populates="video_download")


class AudioGeneration(Base):
    __tablename__ = "audio_generations"
    __table_args__ = (
        Index("ix_audio_generations_status", "status"),
        Index("ix_audio_generations_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("summaries.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, default="elevenlabs", nullable=False)
    voice_id: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_field: Mapped[str] = mapped_column(Text, default="summary_1000", nullable=False)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    summary: Mapped[Summary] = relationship(back_populates="audio_generations")


class AttachmentProcessing(Base):
    __tablename__ = "attachment_processing"
    __table_args__ = (
        Index("ix_attachment_processing_status", "status"),
        Index("ix_attachment_processing_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_text_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vision_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    vision_pages_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    request: Mapped[Request] = relationship(back_populates="attachment")


class UserDevice(Base):
    __tablename__ = "user_devices"
    __table_args__ = (
        Index("ix_user_devices_user_id_platform", "user_id", "platform"),
        Index("ix_user_devices_token", "token", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="devices")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id_client_id", "user_id", "client_id"),
        # Token-family revocation bulk-updates all tokens sharing a family_id.
        Index("ix_refresh_tokens_family_id", "family_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    remember_me: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Token-family rotation: every refresh token belongs to a family;
    # rotation issues a new token in the same family; reuse of a
    # retired token revokes the whole family (see
    # app/security/token_family_policy.py). Migration 0016 adds the
    # backing DB columns; existing rows backfill family_id with each
    # row's own uuid so the constraint stays NOT NULL.
    family_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class XCategory(enum.StrEnum):
    """Classification vocabulary populated by ``ft`` at bookmark capture time.

    Mirrored verbatim from ``ft``'s ``bookmarks.db.category`` column. The v2
    vocabulary is closed; new values require a coordinated change in ``ft`` and
    a follow-up migration to extend the CHECK constraint below.
    """

    TOOL = "tool"
    SECURITY = "security"
    TECHNIQUE = "technique"
    LAUNCH = "launch"
    RESEARCH = "research"
    OPINION = "opinion"
    COMMERCE = "commerce"


_X_CATEGORY_VALUES = tuple(member.value for member in XCategory)


class XBookmarkMetadata(Base):
    """Sidecar row for a ``requests`` entry ingested via the x_bookmarks sync path.

    Schema source of truth: ``docs/explanation/x-bookmarks-integration.md`` table at
    lines 60-71. Lifecycle is shared with the parent ``Request`` (cascade delete).
    The ``tweet_text_tsv`` column is a Postgres ``GENERATED ALWAYS AS ... STORED``
    column that the MCP ``x_search`` tool queries via ``ts_rank_cd``.

    No lifecycle columns. Bookmarks are immortal once ingested (see the Q4 design
    answer); the schema cannot express "no longer bookmarked" by design.
    """

    __tablename__ = "x_bookmark_metadata"
    __table_args__ = (
        CheckConstraint(
            "x_category IN (" + ", ".join(f"'{value}'" for value in _X_CATEGORY_VALUES) + ")",
            name="ck_x_bookmark_metadata_category",
        ),
        Index(
            "ix_x_bookmark_metadata_bookmark_external_id",
            "bookmark_external_id",
            unique=True,
        ),
        Index(
            "ix_x_bookmark_metadata_category",
            "x_category",
        ),
        Index(
            "ix_x_bookmark_metadata_tweet_text_tsv",
            "tweet_text_tsv",
            postgresql_using="gin",
        ),
    )

    request_id: Mapped[int] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    bookmark_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    x_category: Mapped[str] = mapped_column(Text, nullable=False)
    tweet_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tweet_text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(tweet_text, ''))",
            persisted=True,
        ),
    )
    tweet_author: Mapped[str | None] = mapped_column(Text, nullable=True)
    tweet_url: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


CORE_MODELS: tuple[type[Base], ...] = (
    User,
    ClientSecret,
    UserCredential,
    UserIdentity,
    MagicLinkToken,
    Chat,
    Request,
    RequestProcessingJob,
    ProgressEvent,
    TelegramMessage,
    CrawlResult,
    LLMCall,
    Summary,
    UserInteraction,
    AuditLog,
    SummaryEmbedding,
    VideoDownload,
    AudioGeneration,
    AttachmentProcessing,
    UserDevice,
    RefreshToken,
    XBookmarkMetadata,
)

__all__ = [
    "CORE_MODELS",
    "AttachmentProcessing",
    "AudioGeneration",
    "AuditLog",
    "Chat",
    "ClientSecret",
    "CrawlResult",
    "LLMAttemptTrigger",
    "LLMCall",
    "MagicLinkToken",
    "ProgressEvent",
    "RefreshToken",
    "Request",
    "RequestProcessingJob",
    "Summary",
    "SummaryEmbedding",
    "TelegramMessage",
    "User",
    "UserCredential",
    "UserDevice",
    "UserIdentity",
    "UserInteraction",
    "VideoDownload",
    "XBookmarkMetadata",
    "XCategory",
]
