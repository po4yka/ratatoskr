"""SQLAlchemy models for persisted transcription jobs and artifacts."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - SQLAlchemy resolves string annotations at runtime.
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JSONB, _utcnow


class TranscriptionJob(Base):
    __tablename__ = "transcription_jobs"
    __table_args__ = (
        Index("ix_transcription_jobs_user_status", "user_id", "status"),
        Index("ix_transcription_jobs_request_id", "request_id"),
        Index("ix_transcription_jobs_telegram_message", "telegram_chat_id", "telegram_message_id"),
        Index("ix_transcription_jobs_audio_hash", "audio_hash"),
        Index("ix_transcription_jobs_correlation_id", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    backend: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tokens_mode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_identifier: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="started", nullable=False)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    artifacts: Mapped[list[TranscriptionArtifact]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class TranscriptionArtifact(Base):
    __tablename__ = "transcription_artifacts"
    __table_args__ = (
        Index("ix_transcription_artifacts_job_id", "job_id"),
        Index("ix_transcription_artifacts_user_created", "user_id", "created_at"),
        Index("ix_transcription_artifacts_request_id", "request_id"),
        Index("ix_transcription_artifacts_audio_hash", "audio_hash"),
        Index("ix_transcription_artifacts_correlation_id", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("transcription_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    backend: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tokens_mode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_identifier: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="completed", nullable=False)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    plain_text: Mapped[str] = mapped_column(Text, nullable=False)
    sentences_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    speaker_turns_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    audio_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    job: Mapped[TranscriptionJob] = relationship(back_populates="artifacts")


TRANSCRIPTION_MODELS = (TranscriptionJob, TranscriptionArtifact)

__all__ = [
    "TRANSCRIPTION_MODELS",
    "TranscriptionArtifact",
    "TranscriptionJob",
]
