"""SQLAlchemy adapter for persisted transcription jobs and artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.application.ports.transcriptions import (
    TranscriptionArtifactCreate,
    TranscriptionArtifactRecord,
    TranscriptionJobCreate,
    TranscriptionJobRecord,
)
from app.db.models.transcription import TranscriptionArtifact, TranscriptionJob
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class TranscriptionRepositoryAdapter:
    """Transcription persistence backed by SQLAlchemy."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_job(self, job: TranscriptionJobCreate) -> TranscriptionJobRecord:
        row = TranscriptionJob(**_job_values(job))
        async with self._db.transaction() as session:
            session.add(row)
            await session.flush()
            return _job_to_record(row)

    async def complete_job_with_artifact(
        self,
        job_id: int,
        artifact: TranscriptionArtifactCreate,
    ) -> TranscriptionArtifactRecord:
        async with self._db.transaction() as session:
            job = await session.get(TranscriptionJob, job_id)
            if job is None:
                msg = f"transcription job {job_id} does not exist"
                raise ValueError(msg)
            job.status = "completed"
            job.duration_sec = artifact.duration_sec
            job.language = artifact.language or job.language
            job.backend = artifact.backend or job.backend
            job.tokens_mode = artifact.tokens_mode or job.tokens_mode
            job.model_identifier = artifact.model_identifier or job.model_identifier
            job.updated_at = _utcnow()
            row = TranscriptionArtifact(**_artifact_values(artifact))
            session.add(row)
            await session.flush()
            return _artifact_to_record(row)

    async def fail_job(
        self,
        job_id: int,
        *,
        error_code: str,
        error_message: str,
    ) -> TranscriptionJobRecord | None:
        async with self._db.transaction() as session:
            job = await session.get(TranscriptionJob, job_id)
            if job is None:
                return None
            job.status = "failed"
            job.error_code = _safe_text(error_code, max_length=100)
            job.error_message = _safe_text(error_message, max_length=1000)
            job.updated_at = _utcnow()
            await session.flush()
            return _job_to_record(job)

    async def list_artifacts_for_user(
        self,
        user_id: int,
        *,
        limit: int = 50,
    ) -> list[TranscriptionArtifactRecord]:
        bounded_limit = max(1, min(int(limit), 200))
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(TranscriptionArtifact)
                    .where(TranscriptionArtifact.user_id == user_id)
                    .order_by(TranscriptionArtifact.created_at.desc())
                    .limit(bounded_limit)
                )
            ).scalars()
            return [_artifact_to_record(row) for row in rows]


def _job_values(job: TranscriptionJobCreate) -> dict[str, Any]:
    return {
        "user_id": job.user_id,
        "request_id": job.request_id,
        "telegram_chat_id": job.telegram_chat_id,
        "telegram_message_id": job.telegram_message_id,
        "source_type": _safe_text(job.source_type, max_length=100) or "unknown",
        "language": _safe_text(job.language, max_length=32),
        "backend": _safe_text(job.backend, max_length=100),
        "tokens_mode": _safe_text(job.tokens_mode, max_length=100),
        "model_identifier": _safe_text(job.model_identifier, max_length=500),
        "status": _safe_text(job.status, max_length=50) or "started",
        "duration_sec": job.duration_sec,
        "audio_hash": _safe_text(job.audio_hash, max_length=128),
        "correlation_id": _safe_text(job.correlation_id, max_length=128),
        "metadata_json": _sanitize_metadata(job.metadata_json),
    }


def _artifact_values(artifact: TranscriptionArtifactCreate) -> dict[str, Any]:
    return {
        "job_id": artifact.job_id,
        "user_id": artifact.user_id,
        "request_id": artifact.request_id,
        "telegram_chat_id": artifact.telegram_chat_id,
        "telegram_message_id": artifact.telegram_message_id,
        "source_type": _safe_text(artifact.source_type, max_length=100) or "unknown",
        "language": _safe_text(artifact.language, max_length=32),
        "backend": _safe_text(artifact.backend, max_length=100),
        "tokens_mode": _safe_text(artifact.tokens_mode, max_length=100),
        "model_identifier": _safe_text(artifact.model_identifier, max_length=500),
        "status": _safe_text(artifact.status, max_length=50) or "completed",
        "duration_sec": artifact.duration_sec,
        "plain_text": artifact.plain_text,
        "sentences_json": artifact.sentences_json,
        "speaker_turns_json": artifact.speaker_turns_json,
        "audio_hash": _safe_text(artifact.audio_hash, max_length=128),
        "correlation_id": _safe_text(artifact.correlation_id, max_length=128),
        "metadata_json": _sanitize_metadata(artifact.metadata_json),
    }


def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    sanitized: dict[str, Any] = {}
    for key, value in metadata.items():
        safe_key = str(key)
        lowered = safe_key.lower()
        if any(marker in lowered for marker in ("path", "authorization", "token", "secret")):
            continue
        sanitized[safe_key] = _sanitize_value(value)
    return sanitized or None


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_metadata(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_text(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_length]


def _job_to_record(row: TranscriptionJob) -> TranscriptionJobRecord:
    return TranscriptionJobRecord(
        id=row.id,
        user_id=row.user_id,
        request_id=row.request_id,
        telegram_chat_id=row.telegram_chat_id,
        telegram_message_id=row.telegram_message_id,
        source_type=row.source_type,
        language=row.language,
        backend=row.backend,
        tokens_mode=row.tokens_mode,
        model_identifier=row.model_identifier,
        status=row.status,
        duration_sec=row.duration_sec,
        audio_hash=row.audio_hash,
        correlation_id=row.correlation_id,
        error_code=row.error_code,
        error_message=row.error_message,
        metadata_json=row.metadata_json,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _artifact_to_record(row: TranscriptionArtifact) -> TranscriptionArtifactRecord:
    return TranscriptionArtifactRecord(
        id=row.id,
        job_id=row.job_id,
        user_id=row.user_id,
        request_id=row.request_id,
        telegram_chat_id=row.telegram_chat_id,
        telegram_message_id=row.telegram_message_id,
        source_type=row.source_type,
        language=row.language,
        backend=row.backend,
        tokens_mode=row.tokens_mode,
        model_identifier=row.model_identifier,
        status=row.status,
        duration_sec=row.duration_sec,
        plain_text=row.plain_text,
        sentences_json=row.sentences_json,
        speaker_turns_json=row.speaker_turns_json,
        audio_hash=row.audio_hash,
        correlation_id=row.correlation_id,
        metadata_json=row.metadata_json,
        created_at=row.created_at,
    )


__all__ = ["TranscriptionRepositoryAdapter"]
