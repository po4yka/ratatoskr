"""Durable background orchestration for transcription jobs."""

from __future__ import annotations

import asyncio
import shutil
import socket
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.adapters.telegram.transcription_persistence import (
    audio_sha256,
    redact_local_paths,
    sentences_to_json,
    speaker_turns_to_json,
    transcription_model_identifier,
)
from app.adapters.transcription import TranscribeOptions, fetch_url_to_local_sync
from app.application.ports.transcriptions import (
    LeasedTranscriptionJob,
    TranscriptionArtifactCreate,
    TranscriptionJobCreate,
    TranscriptionJobRecord,
)
from app.core.logging_utils import get_logger, log_exception

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.transcription import TranscriptionService
    from app.application.ports.transcriptions import TranscriptionRepositoryPort
    from app.config.transcription import TranscriptionConfig

logger = get_logger(__name__)

TRANSCRIPTION_STAGES: tuple[str, ...] = (
    "queued",
    "downloading_media",
    "probing_audio",
    "decoding_audio",
    "loading_model",
    "transcribing",
    "diarizing",
    "persisting",
    "done",
    "error",
)


@dataclass(frozen=True, slots=True)
class EnqueuedTranscription:
    job: TranscriptionJobRecord
    duplicate: bool


class TranscriptionJobService:
    """Lease, retry, execute, and observe durable transcription jobs."""

    def __init__(
        self,
        *,
        repository: TranscriptionRepositoryPort,
        transcription_service: TranscriptionService,
        cfg: TranscriptionConfig,
        max_attempts: int = 3,
        lease_ttl_seconds: int = 900,
        retry_delay_seconds: int = 60,
        poll_interval_seconds: float = 1.0,
        telegram_media_downloader: Callable[[LeasedTranscriptionJob, Path], Awaitable[Path]]
        | None = None,
        local_media_resolver: Callable[[LeasedTranscriptionJob], Awaitable[Path] | Path]
        | None = None,
    ) -> None:
        self._repo = repository
        self._service = transcription_service
        self._cfg = cfg
        self._max_attempts = max_attempts
        self._lease_ttl_seconds = lease_ttl_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._telegram_media_downloader = telegram_media_downloader
        self._local_media_resolver = local_media_resolver
        self._owner = f"{socket.gethostname()}:{uuid.uuid4().hex}"
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def enqueue_url(
        self,
        *,
        user_id: int,
        source_url: str,
        correlation_id: str | None,
    ) -> EnqueuedTranscription:
        return await self._enqueue(
            TranscriptionJobCreate(
                user_id=user_id,
                source_type="url",
                source_url=source_url,
                idempotency_key=f"url:{user_id}:{source_url}",
                language=self._cfg.language,
                backend=self._cfg.backend,
                tokens_mode=self._cfg.tokens_mode,
                model_identifier=transcription_model_identifier(self._cfg),
                correlation_id=correlation_id,
                max_attempts=self._max_attempts,
                metadata_json={"diarization_enabled": self._cfg.diarization_enabled},
            )
        )

    async def enqueue_telegram_message(
        self,
        *,
        user_id: int,
        source_type: str,
        telegram_chat_id: int | None,
        telegram_message_id: int | None,
        correlation_id: str | None,
    ) -> EnqueuedTranscription:
        idempotency_key = (
            f"telegram:{user_id}:{telegram_chat_id}:{telegram_message_id}"
            if telegram_chat_id is not None and telegram_message_id is not None
            else None
        )
        return await self._enqueue(
            TranscriptionJobCreate(
                user_id=user_id,
                source_type=source_type,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id,
                idempotency_key=idempotency_key,
                language=self._cfg.language,
                backend=self._cfg.backend,
                tokens_mode=self._cfg.tokens_mode,
                model_identifier=transcription_model_identifier(self._cfg),
                correlation_id=correlation_id,
                max_attempts=self._max_attempts,
                metadata_json={"diarization_enabled": self._cfg.diarization_enabled},
            )
        )

    async def enqueue_audio_hash_job(
        self,
        *,
        user_id: int,
        audio_hash: str,
        source_type: str = "local_media",
        correlation_id: str | None = None,
    ) -> EnqueuedTranscription:
        return await self._enqueue(
            TranscriptionJobCreate(
                user_id=user_id,
                source_type=source_type,
                idempotency_key=f"audio:{audio_hash}",
                audio_hash=audio_hash,
                language=self._cfg.language,
                backend=self._cfg.backend,
                tokens_mode=self._cfg.tokens_mode,
                model_identifier=transcription_model_identifier(self._cfg),
                correlation_id=correlation_id,
                max_attempts=self._max_attempts,
            )
        )

    async def _enqueue(self, create: TranscriptionJobCreate) -> EnqueuedTranscription:
        job = await self._repo.enqueue_job(create)
        duplicate = job.attempt_count > 0 or job.status not in {"queued", "failed"}
        await self._repo.append_progress_event(
            job_id=job.id,
            stage="queued",
            status="queued",
            message="Transcription queued",
            progress=0.0,
            payload={"duplicate": duplicate},
            correlation_id=job.correlation_id,
        )
        return EnqueuedTranscription(job=job, duplicate=duplicate)

    async def reconcile_startup(self) -> dict[str, int]:
        return {"requeued": await self._repo.requeue_expired_leases()}

    async def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run_forever(), name="transcription-jobs")
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def run_once(self) -> bool:
        job = await self._repo.lease_next(
            lease_owner=self._owner,
            lease_ttl_seconds=self._lease_ttl_seconds,
        )
        if job is None:
            return False
        await self._process_leased_job(job)
        return True

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_exception(logger, "transcription_worker_failed", exc)
                processed = False
            if not processed:
                await asyncio.sleep(self._poll_interval_seconds)

    async def _process_leased_job(self, job: LeasedTranscriptionJob) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="transcription-job-"))
        try:
            await self._publish(job, "downloading_media", "running", 0.1, "Downloading media")
            media_path = await self._resolve_media(job, workdir)
            audio_hash = job.audio_hash or await audio_sha256(media_path)
            result = await self._service.transcribe_media_path(
                media_path,
                options=TranscribeOptions(with_diarization=self._cfg.diarization_enabled or None),
                correlation_id=job.correlation_id,
                progress_callback=lambda stage, status, progress, message: self._publish(
                    job, stage, status, progress, message
                ),
            )
            await self._publish(job, "persisting", "running", 0.92, "Persisting transcript")
            await self._repo.complete_job_with_artifact(
                job.id,
                TranscriptionArtifactCreate(
                    job_id=job.id,
                    user_id=job.user_id,
                    request_id=job.request_id,
                    telegram_chat_id=job.telegram_chat_id,
                    telegram_message_id=job.telegram_message_id,
                    source_type=job.source_type,
                    language=result.detected_language or self._cfg.language,
                    backend=self._cfg.backend,
                    tokens_mode=self._cfg.tokens_mode,
                    model_identifier=transcription_model_identifier(self._cfg),
                    plain_text=result.plain_text or "",
                    duration_sec=result.duration_sec,
                    sentences_json=sentences_to_json(result),
                    speaker_turns_json=speaker_turns_to_json(result),
                    audio_hash=audio_hash,
                    correlation_id=job.correlation_id,
                    metadata_json={"used_diarization": result.used_diarization},
                ),
            )
            await self._repo.mark_job_succeeded(job.id, lease_owner=self._owner)
            await self._publish(job, "done", "done", 1.0, "Transcription completed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_message = redact_local_paths(str(exc))
            status = await self._repo.mark_leased_job_failed(
                job,
                lease_owner=self._owner,
                error_code=exc.__class__.__name__,
                error_message=safe_message,
                retry_delay_seconds=self._retry_delay_seconds,
            )
            await self._publish(
                job, "error", "error", 1.0 if status == "dead_letter" else None, safe_message
            )
            log_exception(logger, "transcription_job_failed", exc, job_id=job.id)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _resolve_media(self, job: LeasedTranscriptionJob, workdir: Path) -> Path:
        workdir.mkdir(parents=True, exist_ok=True)
        if job.source_type == "url" and job.source_url:
            return await asyncio.to_thread(
                fetch_url_to_local_sync,
                job.source_url,
                workdir,
                correlation_id=job.correlation_id,
            )
        if job.source_type.startswith("telegram_"):
            if self._telegram_media_downloader is None:
                msg = "telegram media downloader is not configured for transcription worker"
                raise RuntimeError(msg)
            return await self._telegram_media_downloader(job, workdir)
        if self._local_media_resolver is not None:
            resolved = self._local_media_resolver(job)
            if hasattr(resolved, "__await__"):
                resolved = await resolved
            return Path(resolved)
        msg = f"unsupported transcription source_type={job.source_type}"
        raise RuntimeError(msg)

    async def _publish(
        self,
        job: LeasedTranscriptionJob,
        stage: str,
        status: str,
        progress: float | None,
        message: str,
    ) -> None:
        await self._repo.append_progress_event(
            job_id=job.id,
            stage=stage,
            status=status,
            message=message,
            progress=progress,
            payload=None,
            correlation_id=job.correlation_id,
        )
