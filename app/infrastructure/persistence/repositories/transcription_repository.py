"""SQLAlchemy adapter for persisted transcription jobs, progress, and artifacts."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.application.ports.transcriptions import (
    LeasedTranscriptionJob,
    TranscriptionArtifactCreate,
    TranscriptionArtifactRecord,
    TranscriptionJobCreate,
    TranscriptionJobRecord,
    TranscriptionProgressEventRecord,
)
from app.db.models.transcription import (
    TranscriptionArtifact,
    TranscriptionJob,
    TranscriptionProgressEvent,
)
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database

_SECRET_QUERY_KEYS = {"access_token", "auth", "code", "key", "secret", "signature", "token"}


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

    async def enqueue_job(self, job: TranscriptionJobCreate) -> TranscriptionJobRecord:
        values = _job_values(job)
        now = _utcnow()
        values.update(
            status="queued",
            current_stage="queued",
            progress=0.0,
            attempt_count=0,
            max_attempts=job.max_attempts,
            lease_owner=None,
            lease_expires_at=None,
            retry_after=now,
            queued_at=now,
            updated_at=now,
            created_at=now,
        )
        async with self._db.transaction() as session:
            if values.get("idempotency_key"):
                row = await session.scalar(
                    insert(TranscriptionJob)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[TranscriptionJob.idempotency_key],
                        set_={
                            "retry_after": now,
                            "updated_at": now,
                            "metadata_json": values["metadata_json"],
                        },
                        where=TranscriptionJob.status.notin_(("done", "dead_letter")),
                    )
                    .returning(TranscriptionJob)
                )
                if row is not None:
                    return _job_to_record(row)
                row = await session.scalar(
                    select(TranscriptionJob).where(
                        TranscriptionJob.idempotency_key == values["idempotency_key"]
                    )
                )
                if row is None:
                    msg = "transcription job enqueue failed"
                    raise RuntimeError(msg)
                return _job_to_record(row)
            row = TranscriptionJob(**values)
            session.add(row)
            await session.flush()
            return _job_to_record(row)

    async def lease_next(
        self, *, lease_owner: str, lease_ttl_seconds: int
    ) -> LeasedTranscriptionJob | None:
        now = _utcnow()
        async with self._db.transaction() as session:
            job = await session.scalar(
                select(TranscriptionJob)
                .where(
                    or_(
                        TranscriptionJob.status == "queued",
                        (
                            (TranscriptionJob.status == "failed")
                            & (
                                (TranscriptionJob.retry_after.is_(None))
                                | (TranscriptionJob.retry_after <= now)
                            )
                        ),
                        (
                            (TranscriptionJob.status == "running")
                            & (TranscriptionJob.lease_expires_at <= now)
                        ),
                    ),
                    TranscriptionJob.attempt_count < TranscriptionJob.max_attempts,
                )
                .order_by(TranscriptionJob.retry_after.asc().nullsfirst(), TranscriptionJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            job.status = "running"
            job.current_stage = "queued"
            job.lease_owner = lease_owner
            job.lease_expires_at = now + timedelta(seconds=lease_ttl_seconds)
            job.attempt_count += 1
            job.started_at = job.started_at or now
            job.updated_at = now
            await session.flush()
            return _leased_job(job)

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
            job.audio_hash = artifact.audio_hash or job.audio_hash
            job.updated_at = _utcnow()
            row = TranscriptionArtifact(**_artifact_values(artifact))
            session.add(row)
            await session.flush()
            return _artifact_to_record(row)

    async def fail_job(
        self, job_id: int, *, error_code: str, error_message: str
    ) -> TranscriptionJobRecord | None:
        async with self._db.transaction() as session:
            job = await session.get(TranscriptionJob, job_id)
            if job is None:
                return None
            job.status = "failed"
            job.current_stage = "error"
            job.error_code = _safe_text(error_code, max_length=100)
            job.error_message = _safe_text(error_message, max_length=1000)
            job.updated_at = _utcnow()
            await session.flush()
            return _job_to_record(job)

    async def mark_job_succeeded(self, job_id: int, *, lease_owner: str) -> None:
        now = _utcnow()
        async with self._db.transaction() as session:
            await session.execute(
                update(TranscriptionJob)
                .where(TranscriptionJob.id == job_id, TranscriptionJob.lease_owner == lease_owner)
                .values(
                    status="done",
                    current_stage="done",
                    progress=1.0,
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_after=None,
                    completed_at=now,
                    error_code=None,
                    error_message=None,
                    updated_at=now,
                )
            )

    async def mark_leased_job_failed(
        self,
        job: LeasedTranscriptionJob,
        *,
        lease_owner: str,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int,
    ) -> str:
        now = _utcnow()
        terminal = job.attempt_count >= job.max_attempts
        status = "dead_letter" if terminal else "failed"
        retry_after = None if terminal else now + timedelta(seconds=retry_delay_seconds)
        async with self._db.transaction() as session:
            await session.execute(
                update(TranscriptionJob)
                .where(TranscriptionJob.id == job.id, TranscriptionJob.lease_owner == lease_owner)
                .values(
                    status=status,
                    current_stage="error",
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_after=retry_after,
                    completed_at=now if terminal else None,
                    error_code=_safe_text(error_code, max_length=100),
                    error_message=_safe_text(error_message, max_length=1000),
                    updated_at=now,
                )
            )
        return status

    async def requeue_expired_leases(self) -> int:
        now = _utcnow()
        async with self._db.transaction() as session:
            result = await session.execute(
                update(TranscriptionJob)
                .where(
                    TranscriptionJob.status == "running",
                    TranscriptionJob.lease_expires_at.is_not(None),
                    TranscriptionJob.lease_expires_at <= now,
                    TranscriptionJob.attempt_count < TranscriptionJob.max_attempts,
                )
                .values(
                    status="queued",
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_after=now,
                    error_code="LEASE_EXPIRED",
                    error_message="Worker lease expired before completion",
                    updated_at=now,
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

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
        now = _utcnow()
        async with self._db.transaction() as session:
            latest_sequence = await session.scalar(
                select(func.max(TranscriptionProgressEvent.sequence)).where(
                    TranscriptionProgressEvent.job_id == job_id
                )
            )
            sequence = int(latest_sequence or 0) + 1
            row = TranscriptionProgressEvent(
                event_id=f"transcription:{job_id}:{sequence}",
                job_id=job_id,
                sequence=sequence,
                stage=_safe_text(stage, max_length=100) or "unknown",
                status=_safe_text(status, max_length=50) or "running",
                message=_safe_text(message, max_length=2000),
                progress=progress,
                payload=_sanitize_metadata(payload),
                correlation_id=_safe_text(correlation_id, max_length=128),
                created_at=now,
            )
            session.add(row)
            values: dict[str, Any] = {
                "current_stage": row.stage,
                "progress": progress,
                "updated_at": now,
            }
            if row.status == "done":
                values["status"] = "done"
            elif row.status not in {"error", "queued"}:
                values["status"] = "running"
            await session.execute(
                update(TranscriptionJob).where(TranscriptionJob.id == job_id).values(**values)
            )
            await session.flush()
            return _progress_to_record(row)

    async def list_progress_events(
        self, job_id: int, *, after_sequence: int = 0, limit: int = 100
    ) -> list[TranscriptionProgressEventRecord]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(TranscriptionProgressEvent)
                    .where(
                        TranscriptionProgressEvent.job_id == job_id,
                        TranscriptionProgressEvent.sequence > after_sequence,
                    )
                    .order_by(TranscriptionProgressEvent.sequence)
                    .limit(max(1, min(int(limit), 500)))
                )
            ).scalars()
            return [_progress_to_record(row) for row in rows]

    async def list_artifacts_for_user(
        self, user_id: int, *, limit: int = 50
    ) -> list[TranscriptionArtifactRecord]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(TranscriptionArtifact)
                    .where(TranscriptionArtifact.user_id == user_id)
                    .order_by(TranscriptionArtifact.created_at.desc())
                    .limit(max(1, min(int(limit), 200)))
                )
            ).scalars()
            return [_artifact_to_record(row) for row in rows]

    async def diagnostics_snapshot(self) -> dict[str, Any]:
        now = _utcnow()
        async with self._db.session() as session:
            rows = await session.execute(
                select(TranscriptionJob.status, func.count(TranscriptionJob.id)).group_by(
                    TranscriptionJob.status
                )
            )
            by_status = {str(status or "unknown"): int(count or 0) for status, count in rows}
            runnable = await session.scalar(
                select(func.count(TranscriptionJob.id)).where(
                    or_(
                        TranscriptionJob.status == "queued",
                        (
                            (TranscriptionJob.status == "failed")
                            & (
                                (TranscriptionJob.retry_after.is_(None))
                                | (TranscriptionJob.retry_after <= now)
                            )
                        ),
                        (
                            (TranscriptionJob.status == "running")
                            & (TranscriptionJob.lease_expires_at <= now)
                        ),
                    ),
                    TranscriptionJob.attempt_count < TranscriptionJob.max_attempts,
                )
            )
            expired_running = await session.scalar(
                select(func.count(TranscriptionJob.id)).where(
                    TranscriptionJob.status == "running",
                    TranscriptionJob.lease_expires_at.is_not(None),
                    TranscriptionJob.lease_expires_at <= now,
                )
            )
            return {
                "by_status": by_status,
                "runnable_count": int(runnable or 0),
                "expired_running_leases": int(expired_running or 0),
                "oldest_queued_at": await session.scalar(
                    select(func.min(TranscriptionJob.queued_at)).where(
                        TranscriptionJob.status == "queued"
                    )
                ),
                "latest_event_at": await session.scalar(
                    select(func.max(TranscriptionProgressEvent.created_at))
                ),
            }


def _job_values(job: TranscriptionJobCreate) -> dict[str, Any]:
    return {
        "user_id": job.user_id,
        "request_id": job.request_id,
        "telegram_chat_id": job.telegram_chat_id,
        "telegram_message_id": job.telegram_message_id,
        "source_url": _safe_url(job.source_url),
        "source_type": _safe_text(job.source_type, max_length=100) or "unknown",
        "idempotency_key": _safe_text(job.idempotency_key, max_length=255),
        "language": _safe_text(job.language, max_length=32),
        "backend": _safe_text(job.backend, max_length=100),
        "tokens_mode": _safe_text(job.tokens_mode, max_length=100),
        "model_identifier": _safe_text(job.model_identifier, max_length=500),
        "status": _safe_text(job.status, max_length=50) or "queued",
        "current_stage": _safe_text(job.current_stage, max_length=100),
        "progress": job.progress,
        "duration_sec": job.duration_sec,
        "audio_hash": _safe_text(job.audio_hash, max_length=128),
        "correlation_id": _safe_text(job.correlation_id, max_length=128),
        "max_attempts": job.max_attempts,
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


def _safe_url(value: Any) -> str | None:
    text = _safe_text(value, max_length=4000)
    if text is None:
        return None
    parts = urlsplit(text)
    query = urlencode(
        [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _SECRET_QUERY_KEYS
        ],
        doseq=True,
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _job_to_record(row: TranscriptionJob) -> TranscriptionJobRecord:
    return TranscriptionJobRecord(
        id=row.id,
        user_id=row.user_id,
        request_id=row.request_id,
        telegram_chat_id=row.telegram_chat_id,
        telegram_message_id=row.telegram_message_id,
        source_url=row.source_url,
        idempotency_key=row.idempotency_key,
        source_type=row.source_type,
        language=row.language,
        backend=row.backend,
        tokens_mode=row.tokens_mode,
        model_identifier=row.model_identifier,
        status=row.status,
        current_stage=row.current_stage,
        progress=row.progress,
        duration_sec=row.duration_sec,
        audio_hash=row.audio_hash,
        correlation_id=row.correlation_id,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        retry_after=row.retry_after,
        queued_at=row.queued_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        error_code=row.error_code,
        error_message=row.error_message,
        metadata_json=row.metadata_json,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _leased_job(row: TranscriptionJob) -> LeasedTranscriptionJob:
    return LeasedTranscriptionJob(
        id=row.id,
        user_id=row.user_id,
        source_type=row.source_type,
        source_url=row.source_url,
        request_id=row.request_id,
        telegram_chat_id=row.telegram_chat_id,
        telegram_message_id=row.telegram_message_id,
        audio_hash=row.audio_hash,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        correlation_id=row.correlation_id,
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


def _progress_to_record(row: TranscriptionProgressEvent) -> TranscriptionProgressEventRecord:
    return TranscriptionProgressEventRecord(
        event_id=row.event_id,
        job_id=row.job_id,
        sequence=row.sequence,
        stage=row.stage,
        status=row.status,
        message=row.message,
        progress=row.progress,
        payload=row.payload,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
    )


__all__ = ["TranscriptionRepositoryAdapter"]
