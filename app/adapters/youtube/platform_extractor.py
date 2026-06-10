"""Platform extractor for YouTube URLs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.platform_extraction.protocol import PlatformExtractor
from app.adapters.youtube.download_pipeline import YouTubeDownloadPipeline
from app.adapters.youtube.feedback_service import YouTubeFeedbackService
from app.adapters.youtube.session_service import YouTubeDownloadSessionService
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.urls.youtube import extract_youtube_video_id, is_youtube_url

if TYPE_CHECKING:
    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
    from app.adapters.content.platform_extraction.models import (
        PlatformExtractionRequest,
        PlatformExtractionResult,
    )
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.transcription import TranscriptionService
    from app.application.ports.requests import RequestRepositoryPort, VideoDownloadRepositoryPort

logger = get_logger(__name__)


class YouTubePlatformExtractor(PlatformExtractor):
    """Extract YouTube content via transcript API, download pipeline, and reuse logic."""

    def __init__(
        self,
        *,
        cfg: Any,
        db: Any,
        response_formatter: ResponseFormatter,
        audit_func: Any,
        lifecycle: PlatformRequestLifecycle,
        request_repo: RequestRepositoryPort,
        video_repo: VideoDownloadRepositoryPort,
        transcription_service: TranscriptionService | None = None,
    ) -> None:
        self._cfg = cfg
        self._feedback_service = YouTubeFeedbackService(response_formatter=response_formatter)
        self._session_service = YouTubeDownloadSessionService(
            cfg=cfg,
            db=db,
            response_formatter=response_formatter,
            audit_func=audit_func,
            lifecycle=lifecycle,
            request_repo=request_repo,
            video_repo=video_repo,
        )
        self._pipeline = YouTubeDownloadPipeline(
            cfg=cfg,
            audit_func=audit_func,
            feedback_service=self._feedback_service,
            session_service=self._session_service,
            transcription_service=transcription_service,
        )

    def supports(self, normalized_url: str) -> bool:
        return is_youtube_url(normalized_url)

    # Behavior verified by test_pipeline_uses_vtt_fallback_when_transcript_api_empty in tests/test_youtube_platform_extractor.py
    async def extract(self, request: PlatformExtractionRequest) -> PlatformExtractionResult:
        if not self._cfg.youtube.enabled:
            raise ValueError("YouTube video download is disabled in configuration")

        video_id = extract_youtube_video_id(request.url_text)
        if not video_id:
            raise ValueError("Invalid YouTube URL: could not extract video ID")

        logger.info(
            "youtube_download_start",
            extra={"video_id": video_id, "url": request.url_text, "cid": request.correlation_id},
        )

        await self._session_service.check_storage_limits()
        preparation = await self._session_service.prepare(request=request, video_id=video_id)
        if preparation.cached_result is not None:
            return preparation.cached_result

        if preparation.wait_for_existing_download:
            existing_download = await self._session_service.await_existing_download_completion(
                req_id=preparation.req_id,
                correlation_id=request.correlation_id,
            )
            return await self._session_service.build_reused_download_result(
                request=request,
                req_id=preparation.req_id,
                download=existing_download,
                reuse_message=(
                    "⏳ Another request is already processing this video. Reusing the result."
                ),
                warning_key="youtube_in_progress_reply_failed",
                missing_transcript_error=(
                    "❌ Reused video download has no transcript/subtitles. Try again later."
                ),
            )

        if preparation.download_id is None:
            msg = "YouTube download preparation produced no download_id"
            raise RuntimeError(msg)

        try:
            return await self._pipeline.run(
                request=request,
                video_id=video_id,
                req_id=preparation.req_id,
                download_id=preparation.download_id,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.exception(
                "youtube_extraction_failed",
                extra={"url": request.url_text, "error": str(exc), "cid": request.correlation_id},
            )
            raise
