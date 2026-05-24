"""Helpers for persisting Telegram transcription outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.application.ports.transcriptions import (
    TranscriptionArtifactCreate,
    TranscriptionJobCreate,
    TranscriptionJobRecord,
    audio_sha256,
    redact_local_paths,
    sentences_to_json,
    speaker_turns_to_json,
    transcription_model_identifier,
)
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from app.adapters.transcription import TranscriptionResult
    from app.application.ports.transcriptions import TranscriptionRepositoryPort
    from app.config.transcription import TranscriptionConfig

logger = get_logger(__name__)

__all__ = [
    "audio_sha256",
    "redact_local_paths",
    "sentences_to_json",
    "speaker_turns_to_json",
    "transcription_model_identifier",
]


@dataclass(frozen=True, slots=True)
class TranscriptionSourceContext:
    user_id: int
    source_type: str
    request_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    correlation_id: str | None = None


async def create_transcription_job(
    repository: TranscriptionRepositoryPort | None,
    *,
    source: TranscriptionSourceContext,
    cfg: TranscriptionConfig,
    media_path: Path,
) -> TranscriptionJobRecord | None:
    if repository is None:
        return None
    audio_hash = await audio_sha256(media_path)
    try:
        return await repository.create_job(
            TranscriptionJobCreate(
                user_id=source.user_id,
                request_id=source.request_id,
                telegram_chat_id=source.telegram_chat_id,
                telegram_message_id=source.telegram_message_id,
                source_type=source.source_type,
                language=cfg.language,
                backend=cfg.backend,
                tokens_mode=cfg.tokens_mode,
                model_identifier=transcription_model_identifier(cfg),
                status="started",
                audio_hash=audio_hash,
                correlation_id=source.correlation_id,
                metadata_json={"diarization_enabled": cfg.diarization_enabled},
            )
        )
    except Exception as exc:
        logger.exception(
            "transcription_job_persist_failed",
            extra={"cid": source.correlation_id, "error": type(exc).__name__},
        )
        return None


async def persist_transcription_artifact(
    repository: TranscriptionRepositoryPort | None,
    *,
    job: TranscriptionJobRecord | None,
    source: TranscriptionSourceContext,
    cfg: TranscriptionConfig,
    result: TranscriptionResult,
) -> None:
    if repository is None or job is None:
        return
    try:
        await repository.complete_job_with_artifact(
            job.id,
            TranscriptionArtifactCreate(
                job_id=job.id,
                user_id=source.user_id,
                request_id=source.request_id,
                telegram_chat_id=source.telegram_chat_id,
                telegram_message_id=source.telegram_message_id,
                source_type=source.source_type,
                language=result.detected_language or cfg.language,
                backend=cfg.backend,
                tokens_mode=cfg.tokens_mode,
                model_identifier=transcription_model_identifier(cfg),
                status="completed",
                duration_sec=result.duration_sec,
                plain_text=result.plain_text or "",
                sentences_json=sentences_to_json(result),
                speaker_turns_json=speaker_turns_to_json(result),
                audio_hash=job.audio_hash,
                correlation_id=source.correlation_id,
                metadata_json={"used_diarization": result.used_diarization},
            ),
        )
    except Exception as exc:
        logger.exception(
            "transcription_artifact_persist_failed",
            extra={"cid": source.correlation_id, "error": type(exc).__name__},
        )


async def mark_transcription_job_failed(
    repository: TranscriptionRepositoryPort | None,
    *,
    job: TranscriptionJobRecord | None,
    correlation_id: str | None,
    error_code: str,
    error_message: str,
) -> None:
    if repository is None or job is None:
        return
    try:
        await repository.fail_job(
            job.id,
            error_code=error_code,
            error_message=redact_local_paths(error_message),
        )
    except Exception as exc:
        logger.exception(
            "transcription_job_fail_persist_failed",
            extra={"cid": correlation_id, "error": type(exc).__name__},
        )


def telegram_chat_id(message: Any) -> int | None:
    chat = getattr(message, "chat", None)
    return _coerce_int(
        getattr(message, "chat_id", None)
        or getattr(message, "peer_id", None)
        or getattr(chat, "id", None)
    )


def telegram_message_id(message: Any) -> int | None:
    return _coerce_int(getattr(message, "id", None) or getattr(message, "message_id", None))


def telegram_user_id(message: Any) -> int | None:
    sender = getattr(message, "sender", None) or getattr(message, "from_user", None)
    return _coerce_int(
        getattr(message, "sender_id", None)
        or getattr(message, "from_id", None)
        or getattr(sender, "id", None)
    )


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None
