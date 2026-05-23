"""/transcribe -- transcribe a URL or a replied-to voice/audio/video message.

Two invocation forms:

    /transcribe <url>          -- fetch the URL via yt-dlp into a temp dir,
                                  decode + recognize, reply with the transcript.
    /transcribe                -- run as a reply to a voice / audio / video_note /
                                  video / document(audio) message; download the
                                  attached media via Telethon, transcribe.

Output:

    * with diarization configured: ``SPEAKER_xx [MM:SS]: text`` lines
    * with timestamps available:   ``[MM:SS] text`` lines
    * otherwise:                   single paragraph of plain text

Transcripts that exceed Telegram's 4096-char limit are uploaded as a ``.txt``
attachment via ``message.reply_document`` (mirrors the export pattern in
``callback_action_io_handlers._send_file``).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.adapters.transcription import (
    AudioDecodeError,
    FfmpegNotInstalledError,
    MediaFetchError,
    NoAudioStreamError,
    Sentence,
    TimestampsUnavailableError,
    TranscribeOptions,
    TranscriptionDisabledError,
    TranscriptionDurationExceededError,
    TranscriptionResult,
    TranscriptionService,
    fetch_url_to_local_sync,
    format_mmss,
)
from app.adapters.transcription.diarization_engine import speaker_at
from app.adapters.telegram.transcription_persistence import (
    TranscriptionSourceContext,
    create_transcription_job,
    mark_transcription_job_failed,
    persist_transcription_artifact,
    telegram_chat_id,
    telegram_message_id,
)
from app.core.logging_utils import get_logger
from app.core.url_utils import extract_all_urls

if TYPE_CHECKING:
    from app.application.services.transcription_job_service import TranscriptionJobService
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.config import AppConfig
    from app.application.ports.transcriptions import TranscriptionRepositoryPort

logger = get_logger(__name__)

_TELEGRAM_TEXT_LIMIT = 4000  # Telegram is 4096; keep headroom for header text.
_TRANSCRIPT_FILENAME = "transcript.txt"


class TranscribeHandler:
    """Handler for the /transcribe Telegram command."""

    def __init__(
        self,
        *,
        cfg: AppConfig,
        response_formatter: ResponseFormatter,
        transcription_service: TranscriptionService,
        transcription_repository: TranscriptionRepositoryPort | None = None,
        transcription_job_service: TranscriptionJobService | None = None,
    ) -> None:
        self._cfg = cfg
        self._formatter = response_formatter
        self._service = transcription_service
        self._transcription_repository = transcription_repository
        self._transcription_job_service = transcription_job_service

    async def handle_transcribe(self, ctx: CommandExecutionContext) -> None:
        """Dispatch /transcribe based on argument shape (URL vs reply)."""
        logger.info(
            "command_transcribe",
            extra={"uid": ctx.uid, "cid": ctx.correlation_id},
        )

        if not self._service.enabled:
            await self._formatter.safe_reply(
                ctx.message,
                "Transcription is disabled. Set TRANSCRIPTION_ENABLED=true to enable it.",
            )
            return

        urls = extract_all_urls(ctx.text or "")
        replied = getattr(ctx.message, "reply_to_message", None)

        if urls:
            await self._transcribe_url(ctx, urls[0])
            return
        if replied is not None and _has_transcribable_media(replied):
            await self._transcribe_reply(ctx, replied)
            return

        await self._formatter.safe_reply(
            ctx.message,
            "Usage: /transcribe <url>, or reply to a voice / audio / video "
            "message with /transcribe.",
        )

    # ------------------------------------------------------------------ URL path

    async def _transcribe_url(self, ctx: CommandExecutionContext, url: str) -> None:
        if self._transcription_job_service is not None:
            queued = await self._transcription_job_service.enqueue_url(
                user_id=int(ctx.uid),
                source_url=url,
                correlation_id=ctx.correlation_id,
            )
            await self._reply_queued(ctx, queued.job.id, duplicate=queued.duplicate)
            return
        await self._formatter.safe_reply(ctx.message, f"Fetching audio from {url}...")
        workdir = Path(tempfile.mkdtemp(prefix="transcribe-"))
        try:
            try:
                media_path = await asyncio.to_thread(
                    fetch_url_to_local_sync,
                    url,
                    workdir,
                    correlation_id=ctx.correlation_id,
                )
            except MediaFetchError as exc:
                await self._reply_error(ctx, f"Could not fetch media: {exc}")
                return
            await self._run_and_reply(ctx, media_path, source_type="url")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ----------------------------------------------------------------- Reply path

    async def _transcribe_reply(
        self,
        ctx: CommandExecutionContext,
        replied: Any,
    ) -> None:
        if self._transcription_job_service is not None:
            queued = await self._transcription_job_service.enqueue_telegram_message(
                user_id=int(ctx.uid),
                source_type="telegram_reply",
                telegram_chat_id=telegram_chat_id(replied),
                telegram_message_id=telegram_message_id(replied),
                correlation_id=ctx.correlation_id,
            )
            await self._reply_queued(ctx, queued.job.id, duplicate=queued.duplicate)
            return
        await self._formatter.safe_reply(ctx.message, "Downloading attached media...")
        workdir = Path(tempfile.mkdtemp(prefix="transcribe-"))
        try:
            try:
                media_path = await _download_telethon_media(replied, workdir)
            except RuntimeError as exc:
                await self._reply_error(ctx, f"Could not download media: {exc}")
                return
            await self._run_and_reply(
                ctx,
                media_path,
                source_type="telegram_reply",
                source_message=replied,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ----------------------------------------------------------- Shared run/reply

    async def _run_and_reply(
        self,
        ctx: CommandExecutionContext,
        media_path: Path,
        *,
        source_type: str,
        source_message: Any | None = None,
    ) -> None:
        await self._formatter.safe_reply(
            ctx.message, "Transcribing (CPU-only; this may take a moment)..."
        )
        options = TranscribeOptions(
            with_diarization=self._cfg.transcription.diarization_enabled or None,
        )
        source = TranscriptionSourceContext(
            user_id=int(ctx.uid),
            source_type=source_type,
            telegram_chat_id=telegram_chat_id(source_message or ctx.message),
            telegram_message_id=telegram_message_id(source_message or ctx.message),
            correlation_id=ctx.correlation_id,
        )
        job = await create_transcription_job(
            self._transcription_repository,
            source=source,
            cfg=self._cfg.transcription,
            media_path=media_path,
        )
        try:
            result = await self._service.transcribe_media_path(
                media_path,
                options=options,
                correlation_id=ctx.correlation_id,
            )
        except TranscriptionDisabledError as exc:
            await mark_transcription_job_failed(
                self._transcription_repository,
                job=job,
                correlation_id=ctx.correlation_id,
                error_code="disabled",
                error_message=str(exc),
            )
            await self._reply_error(ctx, str(exc))
            return
        except TranscriptionDurationExceededError as exc:
            await mark_transcription_job_failed(
                self._transcription_repository,
                job=job,
                correlation_id=ctx.correlation_id,
                error_code="duration_exceeded",
                error_message=str(exc),
            )
            await self._reply_error(
                ctx,
                f"Media is {exc.duration_sec:.0f}s long; the max is "
                f"{exc.max_duration_sec}s (TRANSCRIPTION_MAX_DURATION_SEC).",
            )
            return
        except FfmpegNotInstalledError:
            await mark_transcription_job_failed(
                self._transcription_repository,
                job=job,
                correlation_id=ctx.correlation_id,
                error_code="ffmpeg_not_installed",
                error_message="ffmpeg is not installed",
            )
            await self._reply_error(
                ctx,
                "ffmpeg is not installed on the server; transcription cannot run.",
            )
            return
        except (AudioDecodeError, NoAudioStreamError) as exc:
            await mark_transcription_job_failed(
                self._transcription_repository,
                job=job,
                correlation_id=ctx.correlation_id,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            await self._reply_error(ctx, f"Could not decode audio: {exc}")
            return
        except TimestampsUnavailableError as exc:
            await mark_transcription_job_failed(
                self._transcription_repository,
                job=job,
                correlation_id=ctx.correlation_id,
                error_code="timestamps_unavailable",
                error_message=str(exc),
            )
            await self._reply_error(ctx, str(exc))
            return
        except Exception as exc:
            logger.exception(
                "transcribe_unexpected_failure",
                extra={"cid": ctx.correlation_id, "error": type(exc).__name__},
            )
            await mark_transcription_job_failed(
                self._transcription_repository,
                job=job,
                correlation_id=ctx.correlation_id,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            await self._reply_error(
                ctx,
                f"Transcription failed: {type(exc).__name__}",
            )
            return

        await persist_transcription_artifact(
            self._transcription_repository,
            job=job,
            source=source,
            cfg=self._cfg.transcription,
            result=result,
        )
        await self._send_transcript(ctx, result)

    async def _send_transcript(
        self,
        ctx: CommandExecutionContext,
        result: TranscriptionResult,
    ) -> None:
        body = _format_transcript(result)
        if not body.strip():
            await self._formatter.safe_reply(
                ctx.message,
                "Transcription produced no recognizable speech.",
            )
            return

        if len(body) <= _TELEGRAM_TEXT_LIMIT:
            await self._formatter.safe_reply(ctx.message, body)
            return

        # Long output -- upload as a text attachment.
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
            sent = await _try_reply_document(ctx.message, tmp_path)
            if not sent:
                head = body[: _TELEGRAM_TEXT_LIMIT - 200]
                await self._formatter.safe_reply(
                    ctx.message,
                    head + "\n\n[truncated; could not attach file -- "
                    f"full length was {len(body)} chars]",
                )
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _reply_error(self, ctx: CommandExecutionContext, msg: str) -> None:
        await self._formatter.safe_reply(
            ctx.message,
            f"{msg}\nError ID: {ctx.correlation_id}",
        )

    async def _reply_queued(
        self,
        ctx: CommandExecutionContext,
        job_id: int,
        *,
        duplicate: bool,
    ) -> None:
        prefix = "Transcription is already queued" if duplicate else "Transcription queued"
        await self._formatter.safe_reply(
            ctx.message,
            f"{prefix}. Job ID: {job_id}\nTrace ID: {ctx.correlation_id}",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_transcribable_media(message: Any) -> bool:
    """Return True when ``message`` carries a media attachment we can transcribe."""
    if getattr(message, "voice", None) is not None:
        return True
    if getattr(message, "audio", None) is not None:
        return True
    if getattr(message, "video_note", None) is not None:
        return True
    if getattr(message, "video", None) is not None:
        return True
    document = getattr(message, "document", None)
    if document is not None:
        mime = (getattr(document, "mime_type", "") or "").lower()
        if mime.startswith(("audio/", "video/")):
            return True
    return False


async def _download_telethon_media(message: Any, workdir: Path) -> Path:
    """Download the media attached to ``message`` into ``workdir``.

    Telethon's ``Message.download_media`` returns the path of the saved file.
    Different message wrappers expose it under slightly different names; this
    helper picks whichever is available.
    """
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
    """Try the various Telethon wrappers for sending a file attachment."""
    for attr in ("reply_document", "reply_file"):
        fn = getattr(message, attr, None)
        if fn is None:
            continue
        try:
            await fn(str(path), caption=_TRANSCRIPT_FILENAME)
            return True
        except Exception as exc:
            logger.debug(
                "transcribe_reply_document_failed",
                extra={"attr": attr, "error": type(exc).__name__},
            )
    return False


def _format_transcript(result: TranscriptionResult) -> str:
    """Render a TranscriptionResult into the final user-facing string."""
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


def _sentence_speaker_pairs(
    result: TranscriptionResult,
) -> list[tuple[int | None, Sentence]]:
    """Helper exposed for tests: pair each sentence with its speaker index."""
    return [
        (speaker_at(result.speaker_turns, sentence.start_sec), sentence)
        for sentence in result.sentences
    ]
