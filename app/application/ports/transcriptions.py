"""Ports for persisted transcription jobs and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class TranscriptionJobCreate:
    user_id: int
    source_type: str
    request_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    language: str | None = None
    backend: str | None = None
    tokens_mode: str | None = None
    model_identifier: str | None = None
    status: str = "started"
    duration_sec: float | None = None
    audio_hash: str | None = None
    correlation_id: str | None = None
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionJobRecord:
    id: int
    user_id: int
    request_id: int | None
    telegram_chat_id: int | None
    telegram_message_id: int | None
    source_type: str
    language: str | None
    backend: str | None
    tokens_mode: str | None
    model_identifier: str | None
    status: str
    duration_sec: float | None
    audio_hash: str | None
    correlation_id: str | None
    error_code: str | None
    error_message: str | None
    metadata_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TranscriptionArtifactCreate:
    job_id: int
    user_id: int
    source_type: str
    plain_text: str
    request_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    language: str | None = None
    backend: str | None = None
    tokens_mode: str | None = None
    model_identifier: str | None = None
    status: str = "completed"
    duration_sec: float | None = None
    sentences_json: list[dict[str, Any]] | None = None
    speaker_turns_json: list[dict[str, Any]] | None = None
    audio_hash: str | None = None
    correlation_id: str | None = None
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionArtifactRecord:
    id: int
    job_id: int
    user_id: int
    request_id: int | None
    telegram_chat_id: int | None
    telegram_message_id: int | None
    source_type: str
    language: str | None
    backend: str | None
    tokens_mode: str | None
    model_identifier: str | None
    status: str
    duration_sec: float | None
    plain_text: str
    sentences_json: list[dict[str, Any]] | None
    speaker_turns_json: list[dict[str, Any]] | None
    audio_hash: str | None
    correlation_id: str | None
    metadata_json: dict[str, Any] | None
    created_at: datetime


@runtime_checkable
class TranscriptionRepositoryPort(Protocol):
    """Persistence operations for durable transcription output."""

    async def create_job(self, job: TranscriptionJobCreate) -> TranscriptionJobRecord:
        """Persist a newly started transcription job."""

    async def complete_job_with_artifact(
        self,
        job_id: int,
        artifact: TranscriptionArtifactCreate,
    ) -> TranscriptionArtifactRecord:
        """Mark a job completed and persist its transcript artifact."""

    async def fail_job(
        self,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
    ) -> TranscriptionJobRecord | None:
        """Mark a transcription job failed."""

    async def list_artifacts_for_user(
        self,
        user_id: int,
        *,
        limit: int = 50,
    ) -> list[TranscriptionArtifactRecord]:
        """Return recent persisted transcripts for a user."""
