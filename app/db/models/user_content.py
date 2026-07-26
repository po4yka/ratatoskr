"""User-facing content organization SQLAlchemy models."""

from __future__ import annotations

import datetime as dt  # noqa: TC003
import uuid
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import _next_server_version, _utcnow


class SummaryFeedback(Base):
    __tablename__ = "summary_feedbacks"
    __table_args__ = (
        Index("ix_summary_feedbacks_user_id_summary_id", "user_id", "summary_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    summary_id: Mapped[int] = mapped_column(
        ForeignKey("summaries.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issues: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="summary_feedbacks")
    summary: Mapped[Any] = relationship("Summary", back_populates="feedbacks")


class CustomDigest(Base):
    __tablename__ = "custom_digests"
    __table_args__ = (
        Index("ix_custom_digests_user_id", "user_id"),
        Index("ix_custom_digests_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_ids: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, default="markdown", nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="custom_digests")


class SummaryHighlight(Base):
    __tablename__ = "summary_highlights"
    __table_args__ = (
        Index("ix_summary_highlights_user_id_summary_id", "user_id", "summary_id"),
        Index("ix_summary_highlights_updated_at", "updated_at"),
        Index(
            "ix_summary_highlights_user_server_version_id",
            "user_id",
            "server_version",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    summary_id: Mapped[int] = mapped_column(
        ForeignKey("summaries.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    color: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="highlights")
    summary: Mapped[Any] = relationship("Summary", back_populates="highlights")


class UserGoal(Base):
    __tablename__ = "user_goals"
    __table_args__ = (
        Index("ix_user_goals_user_id_goal_type_scope_type", "user_id", "goal_type", "scope_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    goal_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, default="global", nullable=False)
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="goals")


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        Index("ix_tags_user_id_normalized_name", "user_id", "normalized_name", unique=True),
        Index("ix_tags_user_server_version_id", "user_id", "server_version", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    user: Mapped[Any] = relationship("User", back_populates="tags")
    summary_tags: Mapped[list[Any]] = relationship(
        "SummaryTag", back_populates="tag", cascade="all, delete-orphan"
    )


class SummaryTag(Base):
    __tablename__ = "summary_tags"
    __table_args__ = (
        Index("ix_summary_tags_summary_id_tag_id", "summary_id", "tag_id", unique=True),
        Index("ix_summary_tags_tag_id", "tag_id"),
        Index("ix_summary_tags_server_version_id", "server_version", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(
        ForeignKey("summaries.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(Text, default="manual", nullable=False)
    server_version: Mapped[int] = mapped_column(
        BigInteger, default=_next_server_version, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    summary: Mapped[Any] = relationship("Summary", back_populates="summary_tags")
    tag: Mapped[Tag] = relationship(back_populates="summary_tags")


USER_CONTENT_MODELS = (SummaryFeedback, CustomDigest, SummaryHighlight, UserGoal, Tag, SummaryTag)
