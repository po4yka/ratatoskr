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
        Index("ix_transcription_jobs_idempotency_key", "idempotency_key", unique=True),
        Index("ix_transcription_jobs_status_retry", "status", "retry_after"),
        Index("ix_transcription_jobs_lease_expires_at", "lease_expires_at"),
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
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    backend: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tokens_mode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_identifier: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="started", nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_after: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    progress_events: Mapped[list[TranscriptionProgressEvent]] = relationship(
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


class TranscriptionProgressEvent(Base):
    __tablename__ = "transcription_progress_events"
    __table_args__ = (
        Index("ix_transcription_progress_events_job_sequence", "job_id", "sequence", unique=True),
        Index("ix_transcription_progress_events_event_id", "event_id", unique=True),
        Index("ix_transcription_progress_events_correlation_id", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("transcription_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    job: Mapped[TranscriptionJob] = relationship(back_populates="progress_events")


TRANSCRIPTION_MODELS = (TranscriptionJob, TranscriptionArtifact, TranscriptionProgressEvent)

__all__ = [
    "TRANSCRIPTION_MODELS",
    "TranscriptionArtifact",
    "TranscriptionJob",
    "TranscriptionProgressEvent",
]
