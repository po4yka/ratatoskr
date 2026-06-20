"""Auto-transcribe Telegram voice / audio / video_note messages.

Triggered from ``MessageContentRouter`` when no other handler claims the
message and ``cfg.transcription.enabled and cfg.transcription.auto_on_voice_message``
is set. The user gets a transcript plus the normal structured summary response.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.transcription_persistence import (
    TranscriptionSourceContext,
    create_transcription_job,
    mark_transcription_job_failed,
    persist_transcription_artifact,
    telegram_chat_id,
    telegram_message_id,
    telegram_user_id,
)
from app.adapters.transcription import (
    AudioDecodeError,
    FfmpegNotInstalledError,
    NoAudioStreamError,
    TimestampsUnavailableError,
    TranscribeOptions,
    TranscriptionDisabledError,
    TranscriptionDurationExceededError,
    TranscriptionResult,
    format_mmss,
)
from app.adapters.transcription.diarization_engine import speaker_at
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.content.graph_url_processor import GraphURLProcessor
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.transcription import TranscriptionService
    from app.application.ports.transcriptions import TranscriptionRepositoryPort
    from app.application.services.transcription_job_service import TranscriptionJobService
    from app.config.transcription import TranscriptionConfig

logger = get_logger(__name__)

_TELEGRAM_TEXT_LIMIT = 4000
_TRANSCRIPT_FILENAME = "transcript.txt"
_AUDIO_DOCUMENT_MIME_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
    }
)
_AUDIO_DOCUMENT_SUFFIXES = (".mp3", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".webm")


def has_transcribable_voice_media(message: Any) -> bool:
    """Return True when ``message`` carries voice / audio / video_note media."""
    if (
        getattr(message, "voice", None) is not None
        or getattr(message, "audio", None) is not None
        or getattr(message, "video_note", None) is not None
    ):
        return True
    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "").lower()
        if mime in _AUDIO_DOCUMENT_MIME_TYPES or mime.startswith("audio/"):
            return True
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", "") or getattr(message, "file_name", "") or "").lower()
    return name.endswith(_AUDIO_DOCUMENT_SUFFIXES)


class VoiceMessageProcessor:
    """Download a voice/audio/video_note attachment and reply with its transcript."""

    def __init__(
        self,
        *,
        response_formatter: ResponseFormatter,
        transcription_service: TranscriptionService,
        diarization_enabled: bool,
        transcription_cfg: TranscriptionConfig | None = None,
        transcription_repository: TranscriptionRepositoryPort | None = None,
        transcription_job_service: TranscriptionJobService | None = None,
        summary_processor: GraphURLProcessor | None = None,
    ) -> None:
        self._formatter = response_formatter
        self._service = transcription_service
        self._diarization_enabled = diarization_enabled
        self._transcription_cfg = transcription_cfg
        self._transcription_repository = transcription_repository
        self._transcription_job_service = transcription_job_service
        self._summary_processor = summary_processor

    async def handle(
        self,
        message: Any,
        *,
        correlation_id: str,
    ) -> bool:
        """Process ``message``. Returns True if it was handled (transcript sent)."""
        if not has_transcribable_voice_media(message):
            return False
        if not self._service.enabled:
            return False

        logger.info(
            "voice_message_transcribe_start",
            extra={"cid": correlation_id},
        )
        if self._transcription_job_service is not None:
            source = self._source_context(message, correlation_id)
            if source is None:
                return False
            queued = await self._transcription_job_service.enqueue_telegram_message(
                user_id=source.user_id,
                source_type=source.source_type,
                telegram_chat_id=source.telegram_chat_id,
                telegram_message_id=source.telegram_message_id,
                correlation_id=correlation_id,
            )
            prefix = (
                "Voice transcription is already queued"
                if queued.duplicate
                else "Voice transcription queued"
            )
            await self._formatter.safe_reply(
                message,
                f"{prefix}. Job ID: {queued.job.id}\nTrace ID: {correlation_id}",
            )
            return True
        workdir = Path(tempfile.mkdtemp(prefix="voice-transcribe-"))
        try:
            try:
                media_path = await _download_attached_media(message, workdir)
            except RuntimeError as exc:
                await self._reply_error(
                    message, f"Could not download voice media: {exc}", correlation_id
                )
                return True

            options = TranscribeOptions(with_diarization=self._diarization_enabled or None)
            source = self._source_context(message, correlation_id)
            if source is not None and self._summary_processor is not None:
                req_id = await self._summary_processor.create_text_request(
                    message=message,
                    request_type="telegram_voice",
                    correlation_id=correlation_id,
                )
                source = TranscriptionSourceContext(
                    user_id=source.user_id,
                    source_type=source.source_type,
                    request_id=req_id,
                    telegram_chat_id=source.telegram_chat_id,
                    telegram_message_id=source.telegram_message_id,
                    correlation_id=source.correlation_id,
                )
            job = None
            if source is not None and self._transcription_cfg is not None:
                job = await create_transcription_job(
                    self._transcription_repository,
                    source=source,
                    cfg=self._transcription_cfg,
                    media_path=media_path,
                )
            try:
                result = await self._service.transcribe_media_path(
                    media_path,
                    options=options,
                    correlation_id=correlation_id,
                )
            except TranscriptionDisabledError as exc:
                await mark_transcription_job_failed(
                    self._transcription_repository,
                    job=job,
                    correlation_id=correlation_id,
                    error_code="disabled",
                    error_message=str(exc),
                )
                await self._reply_error(message, str(exc), correlation_id)
                return True
            except TranscriptionDurationExceededError as exc:
                await mark_transcription_job_failed(
                    self._transcription_repository,
                    job=job,
                    correlation_id=correlation_id,
                    error_code="duration_exceeded",
                    error_message=str(exc),
                )
                await self._reply_error(
                    message,
                    f"Voice message is {exc.duration_sec:.0f}s long; the max is "
                    f"{exc.max_duration_sec}s (TRANSCRIPTION_MAX_DURATION_SEC).",
                    correlation_id,
                )
                return True
            except FfmpegNotInstalledError:
                await mark_transcription_job_failed(
                    self._transcription_repository,
                    job=job,
                    correlation_id=correlation_id,
                    error_code="ffmpeg_not_installed",
                    error_message="ffmpeg is not installed",
                )
                await self._reply_error(
                    message,
                    "ffmpeg is not installed on the server; transcription cannot run.",
                    correlation_id,
                )
                return True
            except (AudioDecodeError, NoAudioStreamError) as exc:
                await mark_transcription_job_failed(
                    self._transcription_repository,
                    job=job,
                    correlation_id=correlation_id,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                await self._reply_error(message, f"Could not decode audio: {exc}", correlation_id)
                return True
            except TimestampsUnavailableError as exc:
                await mark_transcription_job_failed(
                    self._transcription_repository,
                    job=job,
                    correlation_id=correlation_id,
                    error_code="timestamps_unavailable",
                    error_message=str(exc),
                )
                await self._reply_error(message, str(exc), correlation_id)
                return True
            except Exception as exc:
                logger.exception(
                    "voice_message_transcribe_failed",
                    extra={"cid": correlation_id, "error": type(exc).__name__},
                )
                await mark_transcription_job_failed(
                    self._transcription_repository,
                    job=job,
                    correlation_id=correlation_id,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                await self._reply_error(
                    message,
                    f"Transcription failed: {type(exc).__name__}",
                    correlation_id,
                )
                return True

            if source is not None and self._transcription_cfg is not None:
                await persist_transcription_artifact(
                    self._transcription_repository,
                    job=job,
                    source=source,
                    cfg=self._transcription_cfg,
                    result=result,
                )
            await self._send_transcript(message, result, correlation_id)
            if (
                source is not None
                and source.request_id is not None
                and self._summary_processor is not None
                and (result.plain_text or "").strip()
            ):
                await self._summary_processor.summarize_text_request(
                    message=message,
                    request_id=source.request_id,
                    content_text=_format_transcript(result),
                    correlation_id=correlation_id,
                    request_type="telegram_voice",
                )
            return True
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _source_context(
        self,
        message: Any,
        correlation_id: str,
    ) -> TranscriptionSourceContext | None:
        user_id = telegram_user_id(message)
        if user_id is None:
            logger.warning(
                "voice_message_transcription_missing_user_id",
                extra={"cid": correlation_id},
            )
            return None
        return TranscriptionSourceContext(
            user_id=user_id,
            source_type="telegram_voice",
            telegram_chat_id=telegram_chat_id(message),
            telegram_message_id=telegram_message_id(message),
            correlation_id=correlation_id,
        )

    async def _send_transcript(
        self,
        message: Any,
        result: TranscriptionResult,
        correlation_id: str,
    ) -> None:
        body = _format_transcript(result)
        if not body.strip():
            await self._formatter.safe_reply(
                message,
                "Voice message produced no recognizable speech.",
            )
            return

        if len(body) <= _TELEGRAM_TEXT_LIMIT:
            await self._formatter.safe_reply(message, body)
            return

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="transcript-",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write(body + "\n")
            tmp_path = Path(fh.name)
        try:
            sent = await _try_reply_document(message, tmp_path)
            if not sent:
                head = body[: _TELEGRAM_TEXT_LIMIT - 200]
                await self._formatter.safe_reply(
                    message,
                    head + "\n\n[truncated; could not attach file -- "
                    f"full length was {len(body)} chars]",
                )
        finally:
            tmp_path.unlink(missing_ok=True)
        logger.info(
            "voice_message_transcribe_done",
            extra={"cid": correlation_id, "chars": len(body)},
        )

    async def _reply_error(
        self,
        message: Any,
        text: str,
        correlation_id: str,
    ) -> None:
        await self._formatter.safe_reply(
            message,
            f"{text}\nError ID: {correlation_id}",
        )


# ---------------------------------------------------------------------------
# Helpers (kept module-level so both this processor and ad-hoc tests can use them)
# ---------------------------------------------------------------------------


async def _download_attached_media(message: Any, workdir: Path) -> Path:
    """Download ``message``'s attached media into ``workdir`` via Telethon."""
    workdir.mkdir(parents=True, exist_ok=True)
    target = str(workdir) + "/"  # trailing slash -> Telethon picks a filename

    download = getattr(message, "download_media", None) or getattr(
        message, "download_attachment", None
    )
    if download is None:
        msg = "this message wrapper does not expose download_media"
        raise RuntimeError(msg)

    saved = (
        await download(file=target) if asyncio.iscoroutinefunction(download) else download(target)
    )
    if asyncio.iscoroutine(saved):
        saved = await saved
    if saved is None:
        msg = "Telethon returned no file path after download_media"
        raise RuntimeError(msg)
    return Path(saved)


async def _try_reply_document(message: Any, path: Path) -> bool:
    for attr in ("reply_document", "reply_file"):
        fn = getattr(message, attr, None)
        if fn is None:
            continue
        try:
            await fn(str(path), caption=_TRANSCRIPT_FILENAME)
            return True
        except Exception as exc:
            logger.debug(
                "voice_reply_document_failed",
                extra={"attr": attr, "error": type(exc).__name__},
            )
    return False


def _format_transcript(result: TranscriptionResult) -> str:
    if result.used_diarization and result.sentences and result.speaker_turns:
        return _format_diarized(result)
    if result.sentences:
        return "\n".join(
            f"[{format_mmss(sentence.start_sec)}] {sentence.text}" for sentence in result.sentences
        )
    return result.plain_text or ""


def _format_diarized(result: TranscriptionResult) -> str:
    lines: list[str] = []
    for sentence in result.sentences:
        speaker = speaker_at(result.speaker_turns, sentence.start_sec)
        label = f"SPEAKER_{speaker:02d}" if speaker is not None else "SPEAKER_??"
        lines.append(f"{label} [{format_mmss(sentence.start_sec)}]: {sentence.text}")
    return "\n".join(lines)


__all__ = [
    "VoiceMessageProcessor",
    "has_transcribable_voice_media",
]
