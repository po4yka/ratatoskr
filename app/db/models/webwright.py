"""SQLAlchemy 2.0 models for the Webwright `/browse` subsystem.

Two tables:

- ``webwright_runs``: one row per ``/browse`` invocation. Final answer,
  trajectory path, cost/steps metrics, and status are all stored here so the
  Telegram surface can re-render a past run and operators can audit cost
  growth without scraping logs.
- ``user_browser_sessions``: per-user, per-domain encrypted cookie blobs so
  Webwright can re-enter authenticated sessions across runs. Reuses the
  existing Fernet key from ``GITHUB_TOKEN_ENCRYPTION_KEY`` via
  ``app.security.secret_crypto`` — one rotation surface for all stored
  secrets.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
import enum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SQLEnum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JSONB, _utcnow


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class WebwrightRunStatus(enum.StrEnum):
    """Lifecycle states for a single Webwright run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class WebwrightRun(Base):
    """One Webwright `/task` invocation initiated by `/browse`."""

    __tablename__ = "webwright_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("requests.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_domains_json: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[WebwrightRunStatus] = mapped_column(
        SQLEnum(
            WebwrightRunStatus,
            name="webwright_run_status",
            values_callable=_enum_values,
            create_type=True,
        ),
        default=WebwrightRunStatus.PENDING,
        nullable=False,
    )
    steps_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    trajectory_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    screenshots_json: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserBrowserSession(Base):
    """Per-user, per-domain encrypted cookie jar for Webwright runs.

    The encrypted_cookies blob is a Fernet ciphertext over the JSON dump of
    a cookie jar. Plaintext never lands on disk; only the sidecar receives
    the decrypted payload at task time, and only over the internal Docker
    network.
    """

    __tablename__ = "user_browser_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "domain", name="uq_user_browser_sessions_user_domain"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_cookies: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


WEBWRIGHT_MODELS = (WebwrightRun, UserBrowserSession)

__all__ = [
    "WEBWRIGHT_MODELS",
    "UserBrowserSession",
    "WebwrightRun",
    "WebwrightRunStatus",
]
