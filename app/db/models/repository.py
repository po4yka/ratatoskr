"""SQLAlchemy 2.0 models for GitHub repository ingestion."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
import enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JSONB, _utcnow


class RepoSource(enum.StrEnum):
    """How this repository was added to the user's collection."""

    MANUAL = "manual"
    STARRED = "starred"


class GitHubAuthMethod(enum.StrEnum):
    """Authentication method used to connect the GitHub integration."""

    PAT = "pat"
    OAUTH_DEVICE = "oauth_device"


class GitHubIntegrationStatus(enum.StrEnum):
    """Current health status of the GitHub integration token."""

    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("user_id", "github_id", name="uq_repositories_user_github"),
        Index("ix_repositories_user_starred", "user_id", "is_starred"),
        Index("ix_repositories_user_language", "user_id", "primary_language"),
        Index(
            "ix_repositories_user_pushed_desc",
            "user_id",
            text("pushed_at DESC NULLS LAST"),
            postgresql_where=text("pushed_at IS NOT NULL"),
        ),
        Index("ix_repositories_github_id", "github_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(320), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_language: Mapped[str | None] = mapped_column(String(100), nullable=True)
    languages_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )
    topics_json: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True, default=list)
    stars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    forks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watchers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(100), nullable=True)
    license_spdx: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_fork: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pushed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at_github: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    readme_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    readme_etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    analysis_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    analysis_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[RepoSource] = mapped_column(
        SQLEnum(
            RepoSource,
            name="repo_source",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=RepoSource.MANUAL,
        nullable=False,
    )
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    last_synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    pending_analysis: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class RepositoryEmbedding(Base):
    __tablename__ = "repository_embeddings"
    __table_args__ = (
        Index("ix_repository_embeddings_index_status", "index_status"),
        Index("ix_repository_embeddings_last_indexed_at", "last_indexed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("repositories.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    embedding_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_indexed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    index_status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class UserGitHubIntegration(Base):
    __tablename__ = "user_github_integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    auth_method: Mapped[GitHubAuthMethod] = mapped_column(
        SQLEnum(
            GitHubAuthMethod,
            name="github_auth_method",
            values_callable=_enum_values,
            create_type=True,
        ),
        nullable=False,
    )
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_scopes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    github_login: Mapped[str | None] = mapped_column(String(100), nullable=True)
    github_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[GitHubIntegrationStatus] = mapped_column(
        SQLEnum(
            GitHubIntegrationStatus,
            name="github_integration_status",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=GitHubIntegrationStatus.ACTIVE,
        nullable=False,
    )
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sync_cursor: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_full_sync_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notified_needs_reauth_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


REPOSITORY_MODELS = (Repository, RepositoryEmbedding, UserGitHubIntegration)

__all__ = [
    "REPOSITORY_MODELS",
    "GitHubAuthMethod",
    "GitHubIntegrationStatus",
    "RepoSource",
    "Repository",
    "RepositoryEmbedding",
    "UserGitHubIntegration",
]
