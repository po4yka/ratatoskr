"""SQLAlchemy 2.0 models for git mirror/backup storage."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
import enum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import _utcnow


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


class GitMirrorSource(enum.StrEnum):
    """How this mirror was added."""

    GITHUB = "github"
    MANUAL = "manual"


class GitMirrorStatus(enum.StrEnum):
    """Current health state of the mirror."""

    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    EXCLUDED = "excluded"


class GitMirror(Base):
    __tablename__ = "git_mirrors"
    __table_args__ = (
        UniqueConstraint("user_id", "clone_url", name="uq_git_mirrors_user_clone_url"),
        Index("ix_git_mirrors_user_status", "user_id", "status"),
        Index("ix_git_mirrors_repository_id", "repository_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repository_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("repositories.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[GitMirrorSource] = mapped_column(
        SQLEnum(
            GitMirrorSource,
            name="git_mirror_source",
            values_callable=_enum_values,
            create_type=True,
        ),
        nullable=False,
    )
    clone_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    name: Mapped[str | None] = mapped_column(String(320), nullable=True)
    mirror_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[GitMirrorStatus] = mapped_column(
        SQLEnum(
            GitMirrorStatus,
            name="git_mirror_status",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=GitMirrorStatus.PENDING,
        nullable=False,
    )
    default_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_kb: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_mirrored_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    backoff_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    excluded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    clone_strategy: Mapped[str | None] = mapped_column(String(50), nullable=True)
    readme_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    readme_indexed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


GIT_BACKUP_MODELS = (GitMirror,)

__all__ = [
    "GIT_BACKUP_MODELS",
    "GitMirror",
    "GitMirrorSource",
    "GitMirrorStatus",
]
