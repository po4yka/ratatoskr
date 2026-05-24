"""Ports for persisted transcription jobs and artifacts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class TranscriptionJobCreate:
    user_id: int
    source_type: str
    request_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    source_url: str | None = None
    idempotency_key: str | None = None
    language: str | None = None
    backend: str | None = None
    tokens_mode: str | None = None
    model_identifier: str | None = None
    status: str = "queued"
    current_stage: str | None = "queued"
    progress: float | None = 0.0
    duration_sec: float | None = None
    audio_hash: str | None = None
    correlation_id: str | None = None
    max_attempts: int = 3
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionJobRecord:
    id: int
    user_id: int
    request_id: int | None
    telegram_chat_id: int | None
    telegram_message_id: int | None
    source_url: str | None
    idempotency_key: str | None
    source_type: str
    language: str | None
    backend: str | None
    tokens_mode: str | None
    model_identifier: str | None
    status: str
    current_stage: str | None
    progress: float | None
    duration_sec: float | None
    audio_hash: str | None
    correlation_id: str | None
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    retry_after: datetime | None
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
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


@dataclass(frozen=True, slots=True)
class TranscriptionProgressEventRecord:
    event_id: str
    job_id: int
    sequence: int
    stage: str
    status: str
    message: str | None
    progress: float | None
    payload: dict[str, Any] | None
    correlation_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LeasedTranscriptionJob:
    id: int
    user_id: int
    source_type: str
    source_url: str | None
    request_id: int | None
    telegram_chat_id: int | None
    telegram_message_id: int | None
    audio_hash: str | None
    attempt_count: int
    max_attempts: int
    correlation_id: str | None


@runtime_checkable
class TranscriptionRepositoryPort(Protocol):
    """Persistence operations for durable transcription output."""

    async def create_job(self, job: TranscriptionJobCreate) -> TranscriptionJobRecord:
        """Persist a newly started transcription job."""

    async def enqueue_job(self, job: TranscriptionJobCreate) -> TranscriptionJobRecord:
        """Persist or return a non-terminal transcription job for an idempotency key."""

    async def lease_next(
        self,
        *,
        lease_owner: str,
        lease_ttl_seconds: int,
    ) -> LeasedTranscriptionJob | None:
        """Lease the next runnable transcription job."""

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

    async def mark_job_succeeded(self, job_id: int, *, lease_owner: str) -> None:
        """Mark a leased job done."""

    async def mark_leased_job_failed(
        self,
        job: LeasedTranscriptionJob,
        *,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int,
    ) -> str:
        """Mark a leased job failed or terminal after attempts are exhausted."""

    async def requeue_expired_leases(self) -> int:
        """Move expired running jobs back to queued."""

    async def append_progress_event(
        self,
        *,
        job_id: int,
        stage: str,
        status: str,
        message: str | None,
        progress: float | None,
        payload: dict[str, Any] | None,
        correlation_id: str | None,
    ) -> TranscriptionProgressEventRecord:
        """Append a replayable transcription progress event."""

    async def list_progress_events(
        self,
        job_id: int,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[TranscriptionProgressEventRecord]:
        """Return progress events ordered by sequence."""

    async def list_artifacts_for_user(
        self,
        user_id: int,
        *,
        limit: int = 50,
    ) -> list[TranscriptionArtifactRecord]:
        """Return recent persisted transcripts for a user."""


# ---------------------------------------------------------------------------
# Pure value types and helpers that belong in the application layer.
# These were originally co-located with adapter code; they live here so
# application services can import them without crossing the hexagonal boundary.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TranscribeOptions:
    """Per-call knobs that override ``TranscriptionConfig`` defaults.

    Defined here (application ports) so ``TranscriptionJobService`` and other
    application-layer callers can reference it without importing the adapter.
    The adapter re-exports this class unchanged.
    """

    with_diarization: bool | None = None
    speed: float | None = None
    num_speakers: int | None = None


async def audio_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a local audio file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_local_paths(value: str) -> str:
    """Replace absolute filesystem paths with ``[redacted-path]``."""
    return re.sub(r"(?<!\w)/(?:private/)?(?:tmp|var|Users|data)/[^\s:]+", "[redacted-path]", value)


def sentences_to_json(result: Any) -> list[dict[str, Any]]:
    """Serialise ``TranscriptionResult.sentences`` to a plain list of dicts."""
    return [{"start_sec": s.start_sec, "text": s.text} for s in result.sentences]


def speaker_turns_to_json(result: Any) -> list[dict[str, Any]]:
    """Serialise ``TranscriptionResult.speaker_turns`` to a plain list of dicts."""
    return [
        {"start": t.start, "end": t.end, "speaker": t.speaker, "label": t.label}
        for t in result.speaker_turns
    ]


def transcription_model_identifier(cfg: Any) -> str:
    """Build a human-readable model identifier string from a ``TranscriptionConfig``."""
    model_name = cfg.model_path.name or "model"
    return f"{cfg.language}:{cfg.backend}:{cfg.tokens_mode}:{model_name}"
