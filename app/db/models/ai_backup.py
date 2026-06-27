"""SQLAlchemy 2.0 models for AI account backup state.

One row per ``(user_id, service)`` tracks the lifecycle of backing up the
operator's ChatGPT / Claude web account: health state, failure/backoff
counters, last-success timestamp, per-run counts, and the path of the most
recent backup. Modeled on ``GitMirror`` (``app/db/models/git_backup.py``).

The authenticated session itself is NOT stored here — it lives, Fernet-encrypted,
in ``user_browser_sessions`` (``UserBrowserSession``) keyed by ``(user_id, domain)``.
"""

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
from app.db.types import JSONB, _utcnow


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


class AiBackupService(enum.StrEnum):
    """Which AI web account this backup row tracks."""

    CHATGPT = "chatgpt"
    CLAUDE = "claude"


class AiBackupStatus(enum.StrEnum):
    """Current health state of the backup for a service."""

    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    AUTH_EXPIRED = "auth_expired"
    DISABLED = "disabled"


class AiAccountBackup(Base):
    """Per-user, per-service backup lifecycle row."""

    __tablename__ = "ai_account_backups"
    __table_args__ = (
        UniqueConstraint("user_id", "service", name="uq_ai_account_backups_user_service"),
        Index("ix_ai_account_backups_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service: Mapped[AiBackupService] = mapped_column(
        SQLEnum(
            AiBackupService,
            name="ai_backup_service",
            values_callable=_enum_values,
            create_type=True,
        ),
        nullable=False,
    )
    status: Mapped[AiBackupStatus] = mapped_column(
        SQLEnum(
            AiBackupStatus,
            name="ai_backup_status",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=AiBackupStatus.PENDING,
        nullable=False,
    )
    last_backed_up_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    backoff_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_failures: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0, nullable=False
    )
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    counts_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_backup_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


AI_BACKUP_MODELS = (AiAccountBackup,)

__all__ = [
    "AI_BACKUP_MODELS",
    "AiAccountBackup",
    "AiBackupService",
    "AiBackupStatus",
]
