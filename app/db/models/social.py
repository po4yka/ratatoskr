"""SQLAlchemy models for encrypted social provider connections."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
import enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JSONB, _utcnow


class SocialProvider(enum.StrEnum):
    """Supported social connection providers."""

    X = "x"
    INSTAGRAM = "instagram"
    THREADS = "threads"


class SocialAuthType(enum.StrEnum):
    """Authentication shape used by a social connection."""

    OAUTH2 = "oauth2"
    COOKIE = "cookie"
    MANUAL = "manual"


class SocialConnectionStatus(enum.StrEnum):
    """Current usability state for stored social credentials."""

    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"
    DISABLED = "disabled"


class SocialAuthStateStatus(enum.StrEnum):
    """OAuth state lifecycle for future social authorization endpoints."""

    PENDING = "pending"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class SocialFetchAttemptStatus(enum.StrEnum):
    """Fetch attempt outcome for future social synchronization jobs."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


class SocialConnection(Base):
    __tablename__ = "social_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_social_connections_user_provider"),
        Index("ix_social_connections_user_status", "user_id", "status"),
        Index("ix_social_connections_provider_user", "provider", "provider_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[SocialProvider] = mapped_column(
        SQLEnum(
            SocialProvider,
            name="social_provider",
            values_callable=_enum_values,
            create_type=True,
        ),
        nullable=False,
    )
    auth_type: Mapped[SocialAuthType] = mapped_column(
        SQLEnum(
            SocialAuthType,
            name="social_auth_type",
            values_callable=_enum_values,
            create_type=True,
        ),
        nullable=False,
    )
    provider_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_access_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_scopes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    access_token_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_token_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[SocialConnectionStatus] = mapped_column(
        SQLEnum(
            SocialConnectionStatus,
            name="social_connection_status",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=SocialConnectionStatus.ACTIVE,
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class SocialAuthState(Base):
    __tablename__ = "social_auth_states"
    __table_args__ = (
        UniqueConstraint("provider", "state_hash", name="uq_social_auth_states_provider_state"),
        Index("ix_social_auth_states_user_provider", "user_id", "provider"),
        Index("ix_social_auth_states_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[SocialProvider] = mapped_column(
        SQLEnum(
            SocialProvider,
            name="social_provider",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
    )
    state_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_code_verifier: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    redirect_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    scopes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[SocialAuthStateStatus] = mapped_column(
        SQLEnum(
            SocialAuthStateStatus,
            name="social_auth_state_status",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=SocialAuthStateStatus.PENDING,
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class SocialFetchAttempt(Base):
    __tablename__ = "social_fetch_attempts"
    __table_args__ = (
        Index("ix_social_fetch_attempts_connection_started", "connection_id", "started_at"),
        Index("ix_social_fetch_attempts_user_provider", "user_id", "provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("social_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[SocialProvider] = mapped_column(
        SQLEnum(
            SocialProvider,
            name="social_provider",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
    )
    attempt_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[SocialFetchAttemptStatus] = mapped_column(
        SQLEnum(
            SocialFetchAttemptStatus,
            name="social_fetch_attempt_status",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=SocialFetchAttemptStatus.STARTED,
        nullable=False,
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


SOCIAL_MODELS = (SocialConnection, SocialAuthState, SocialFetchAttempt)

__all__ = [
    "SOCIAL_MODELS",
    "SocialAuthState",
    "SocialAuthStateStatus",
    "SocialAuthType",
    "SocialConnection",
    "SocialConnectionStatus",
    "SocialFetchAttempt",
    "SocialFetchAttemptStatus",
    "SocialProvider",
]
