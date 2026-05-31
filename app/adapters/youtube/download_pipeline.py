"""Download and transcript pipeline for YouTube platform extraction."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled, VideoUnavailable

from app.adapters.content.platform_extraction.models import (
    PlatformExtractionRequest,
    PlatformExtractionResult,
)
from app.adapters.video.source_extractor import (
    MetadataDrivenVideoSourceExtractor,
    VideoSourceRequest,
    build_video_controls_from_config,
)
from app.adapters.youtube.youtube_downloader_parts import (
    metadata as _metadata,
    transcript_api as _transcript_api,
    vtt as _vtt,
    yt_dlp_client as _yt_dlp_client,
)
from app.application.dto.aggregation import (
    SourceMediaAsset,
    SourceMediaKind,
)
from app.core.async_utils import raise_if_cancelled
from app.core.lang import detect_language
from app.core.logging_utils import get_logger
from app.core.urls.youtube import extract_youtube_video_id
from app.domain.models.source import SourceItem, SourceKind
from app.observability.attributes import SOURCE_URL

if TYPE_CHECKING:
    from app.adapters.transcription import TranscriptionService
    from app.adapters.youtube.feedback_service import YouTubeFeedbackService
    from app.adapters.youtube.session_service import YouTubeDownloadSessionService

logger = get_logger(__name__)


def _get_tracer() -> Any:
    from app.observability.otel import get_tracer

    return get_tracer(__name__)


_KNOWN_LANG_CODES = _vtt.KNOWN_LANG_CODES


class YouTubeDownloadPipeline:
    """Run transcript extraction, video download, VTT fallback, and persistence."""

    _MAX_TRANSCRIPT_CHARS = 500_000

    def __init__(
        self,
        *,
        cfg: Any,
        audit_func: Any,
        feedback_service: YouTubeFeedbackService,
        session_service: YouTubeDownloadSessionService,
        transcription_service: TranscriptionService | None = None,
    ) -> None:
        self._cfg = cfg
        self._audit = audit_func
        self._feedback_service = feedback_service
        self._session_service = session_service
        self._transcription_service = transcription_service
        self._video_source_extractor = MetadataDrivenVideoSourceExtractor()

    async def run(
        self,
        *,
        request: PlatformExtractionRequest,
        video_id: str,
        req_id: int,
        download_id: int,
    ) -> PlatformExtractionResult:
        output_dir: Path | None = None
        download_succeeded = False
        feedback_state = await self._feedback_service.start(request=request, video_id=video_id)
        try:
            await self._session_service.mark_download_started(download_id)
            (
                transcript_text,
                transcript_lang,
                auto_generated,
                transcript_source,
            ) = await self._extract_transcript_api(video_id, request.correlation_id)
            await self._feedback_service.mark_transcript_ready(
                state=feedback_state,
                request=request,
                video_id=video_id,
            )

            output_dir = self._session_service.storage_path / datetime.now(UTC).strftime("%Y%m%d")
            output_dir.mkdir(parents=True, exist_ok=True)
            ydl_opts = self._get_ydl_opts(video_id, output_dir)
            with _get_tracer().start_as_current_span("youtube.download") as _dl_span:
                _dl_span.set_attribute(SOURCE_URL, request.url_text)
                async with asyncio.timeout(600.0):
                    video_metadata = await asyncio.to_thread(
                        self._download_video_sync,
                        request.url_text,
                        ydl_opts,
                        request.correlation_id,
                    )

            if feedback_state.updater is not None:
                stage_duration = time.time() - feedback_state.stage_start
                feedback_state.completed_stages.append(("Video downloaded", stage_duration))
                feedback_state.stage_start = time.time()

            if not transcript_text:
                await self._feedback_service.mark_subtitle_fallback(
                    state=feedback_state,
                    request=request,
                    video_id=video_id,
                )
                transcript_text, transcript_lang = self._load_transcript_from_vtt(
                    video_metadata.get("subtitle_file_path"),
                    request.correlation_id,
                )
                if transcript_text:
                    transcript_source = "vtt"
                    if feedback_state.updater is not None:
                        stage_duration = time.time() - feedback_state.stage_start
                        feedback_state.completed_stages.append(
                            ("Subtitles processed", stage_duration)
                        )

            audio_transcript_text: str | None = None
            if not transcript_text and self._should_attempt_local_transcription():
                audio_transcript_text = await self._transcribe_video_locally(
                    video_metadata.get("video_file_path"),
                    correlation_id=request.correlation_id,
                )
                if audio_transcript_text:
                    transcript_text = audio_transcript_text
                    transcript_source = "asr_local"
                    if not transcript_lang:
                        transcript_lang = detect_language(audio_transcript_text)
                    if feedback_state.updater is not None:
                        stage_duration = time.time() - feedback_state.stage_start
                        feedback_state.completed_stages.append(
                            ("Local transcription completed", stage_duration)
                        )

            if not transcript_text:
                raise ValueError(
                    f"❌ No transcript or subtitles available for this video. "
                    f"Error ID: {request.correlation_id or 'unknown'}"
                )

            detected_lang = detect_language(transcript_text or "")
            await self._session_service.persist_success(
                req_id=req_id,
                download_id=download_id,
                video_metadata=video_metadata,
                transcript_text=transcript_text,
                transcript_lang=transcript_lang,
                auto_generated=auto_generated,
                transcript_source=transcript_source,
                detected_lang=detected_lang,
            )
            await self._feedback_service.finalize_success(
                state=feedback_state,
                request=request,
                video_metadata=video_metadata,
            )
            self._audit(
                "INFO",
                "youtube_download_complete",
                {
                    "video_id": video_id,
                    "request_id": req_id,
                    "download_id": download_id,
                    "file_size_mb": video_metadata["file_size"] / (1024 * 1024),
                    "cid": request.correlation_id,
                },
            )
            download_succeeded = True
            source_item = SourceItem.create(
                kind=SourceKind.YOUTUBE_VIDEO,
                original_value=request.url_text,
                normalized_value=request.normalized_url,
                external_id=video_id,
                request_id=req_id,
                title_hint=video_metadata.get("title"),
                metadata={"platform": "youtube"},
            )
            existing_media: list[SourceMediaAsset] = []
            video_file_path = str(video_metadata.get("video_file_path") or "").strip()
            if video_file_path:
                existing_media.append(
                    SourceMediaAsset(
                        kind=SourceMediaKind.VIDEO,
                        local_path=video_file_path,
                        duration_sec=float(video_metadata.get("duration") or 0) or None,
                        metadata={"platform": "youtube"},
                    )
                )
            thumbnail_file_path = str(video_metadata.get("thumbnail_file_path") or "").strip()
            if thumbnail_file_path:
                existing_media.append(
                    SourceMediaAsset(
                        kind=SourceMediaKind.IMAGE,
                        local_path=thumbnail_file_path,
                        metadata={"platform": "youtube", "role": "thumbnail"},
                    )
                )
            result = self._video_source_extractor.extract(
                VideoSourceRequest(
                    source_item=source_item,
                    platform="youtube",
                    title=str(video_metadata.get("title") or "").strip() or None,
                    body_text=_metadata.format_metadata_header(video_metadata),
                    transcript_text=transcript_text,
                    transcript_source=transcript_source,
                    audio_transcript_text=audio_transcript_text,
                    content_source=transcript_source,
                    content_text_override=_metadata.combine_metadata_and_transcript(
                        video_metadata, transcript_text
                    ),
                    detected_language=detected_lang,
                    duration_sec=float(video_metadata.get("duration") or 0) or None,
                    existing_media=tuple(existing_media),
                    metadata=video_metadata,
                    controls=build_video_controls_from_config(self._cfg),
                )
            )
            return PlatformExtractionResult(
                platform="youtube",
                request_id=req_id,
                content_text=result.content_text,
                content_source=result.content_source,
                detected_lang=result.normalized_document.detected_language or detected_lang,
                title=video_metadata.get("title"),
                images=result.images,
                metadata=result.metadata,
                source_item=source_item,
                normalized_document=result.normalized_document,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            await self._session_service.handle_failure(
                req_id=req_id,
                download_id=download_id,
                video_id=video_id,
                error=exc,
                correlation_id=request.correlation_id,
            )
            await self._feedback_service.finalize_error(
                state=feedback_state,
                error=exc,
                correlation_id=request.correlation_id,
            )
            raise
        finally:
            if output_dir is None and not download_succeeded:
                candidate = self._session_service.storage_path / datetime.now(UTC).strftime(
                    "%Y%m%d"
                )
                if candidate.exists():
                    output_dir = candidate
            if not download_succeeded and output_dir is not None:
                self._session_service.cleanup_partial_download_files(
                    output_dir=output_dir,
                    video_id=video_id,
                    correlation_id=request.correlation_id,
                )

    async def _extract_transcript_api(
        self,
        video_id: str,
        correlation_id: str | None,
    ) -> tuple[str, str, bool, str]:
        return await _transcript_api.extract_transcript_via_api(
            video_id=video_id,
            preferred_langs=self._cfg.youtube.subtitle_languages,
            correlation_id=correlation_id,
            youtube_transcript_api=YouTubeTranscriptApi,
            no_transcript_found_exc=NoTranscriptFound,
            transcripts_disabled_exc=TranscriptsDisabled,
            video_unavailable_exc=VideoUnavailable,
            raise_if_cancelled=raise_if_cancelled,
            max_transcript_chars=self._MAX_TRANSCRIPT_CHARS,
            log=logger,
        )

    def _load_transcript_from_vtt(
        self,
        subtitle_path: str | None,
        correlation_id: str | None,
    ) -> tuple[str, str]:
        if not subtitle_path:
            return "", ""
        try:
            text, lang = _vtt.parse_vtt_file(
                Path(subtitle_path), known_lang_codes=_KNOWN_LANG_CODES
            )
            if text:
                logger.info(
                    "youtube_transcript_vtt_loaded",
                    extra={"subtitle_lang": lang, "cid": correlation_id},
                )
            return text, lang or ""
        except FileNotFoundError:
            logger.warning(
                "youtube_transcript_vtt_missing",
                extra={"subtitle_path": subtitle_path, "cid": correlation_id},
            )
        except Exception as exc:
            logger.warning(
                "youtube_transcript_vtt_parse_failed",
                extra={"subtitle_path": subtitle_path, "error": str(exc), "cid": correlation_id},
            )
        return "", ""

    def _get_ydl_opts(self, video_id: str, output_path: Path) -> dict[str, Any]:
        return _yt_dlp_client.build_ydl_opts(
            video_id=video_id,
            output_path=output_path,
            preferred_quality=self._cfg.youtube.preferred_quality,
            subtitle_languages=self._cfg.youtube.subtitle_languages,
            max_video_size_mb=self._cfg.youtube.max_video_size_mb,
        )

    def _download_video_sync(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        correlation_id: str | None,
    ) -> dict[str, Any]:
        return _yt_dlp_client.download_video_sync(
            url=url,
            ydl_opts=ydl_opts,
            subtitle_languages=self._cfg.youtube.subtitle_languages,
            correlation_id=correlation_id,
            extract_youtube_video_id=extract_youtube_video_id,
            yt_dlp_module=yt_dlp,
        )

    def _should_attempt_local_transcription(self) -> bool:
        """Local CPU transcription is opt-in and only runs when no caption path produced text."""
        if self._transcription_service is None:
            return False
        if not self._transcription_service.enabled:
            return False
        return bool(getattr(self._cfg.transcription, "auto_in_url_pipeline", False))

    async def _transcribe_video_locally(
        self,
        video_file_path: str | None,
        *,
        correlation_id: str | None,
    ) -> str | None:
        """Run sherpa-onnx ASR on a downloaded video file. Best-effort: failures
        are logged and turned into ``None`` rather than raised, so the caller's
        existing "no transcript" error still fires when both paths are exhausted.
        """
        if not video_file_path or self._transcription_service is None:
            return None
        media_path = Path(video_file_path)
        if not media_path.is_file():
            logger.warning(
                "youtube_transcription_fallback_missing_file",
                extra={"path": video_file_path, "cid": correlation_id},
            )
            return None
        logger.info(
            "youtube_transcription_fallback_start",
            extra={"path": video_file_path, "cid": correlation_id},
        )
        try:
            result = await self._transcription_service.transcribe_media_path(
                media_path,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "youtube_transcription_fallback_failed",
                extra={"path": video_file_path, "error": str(exc), "cid": correlation_id},
            )
            return None
        text = (result.plain_text or "").strip()
        if not text:
            logger.info(
                "youtube_transcription_fallback_empty",
                extra={"path": video_file_path, "cid": correlation_id},
            )
            return None
        logger.info(
            "youtube_transcription_fallback_complete",
            extra={"path": video_file_path, "chars": len(text), "cid": correlation_id},
        )
        return text
