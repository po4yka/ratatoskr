from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import yt_dlp
except ModuleNotFoundError:

    class _DownloadError(Exception):
        pass

    class _FallbackYoutubeDL:
        def __init__(self, *args, **kwargs):
            self._opts = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, *args, **kwargs):
            return {}

        def download(self, *args, **kwargs):
            return None

        def prepare_filename(self, _info):
            return "/tmp/fallback.mp4"

    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = _FallbackYoutubeDL  # type: ignore[attr-defined]
    yt_dlp.utils = types.SimpleNamespace(DownloadError=_DownloadError)  # type: ignore[attr-defined]
    sys.modules["yt_dlp"] = yt_dlp

try:
    from youtube_transcript_api import YouTubeTranscriptApi  # noqa: F401
except ModuleNotFoundError:

    class NoTranscriptFound(Exception):
        pass

    class TranscriptsDisabled(Exception):
        pass

    class VideoUnavailable(Exception):
        pass

    sys.modules["youtube_transcript_api"] = MagicMock(YouTubeTranscriptApi=MagicMock())
    sys.modules["youtube_transcript_api._errors"] = MagicMock(
        NoTranscriptFound=NoTranscriptFound,
        TranscriptsDisabled=TranscriptsDisabled,
        VideoUnavailable=VideoUnavailable,
    )

from app.adapters.content.platform_extraction.models import (
    PlatformExtractionRequest,
    PlatformExtractionResult,
)
from app.adapters.youtube.download_pipeline import YouTubeDownloadPipeline
from app.adapters.youtube.feedback_service import YouTubeFeedbackService
from app.adapters.youtube.platform_extractor import YouTubePlatformExtractor
from app.adapters.youtube.session_service import (
    YouTubeDownloadPreparation,
    YouTubeDownloadSessionService,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_cfg(tmp_path: Path) -> Any:
    return SimpleNamespace(
        youtube=SimpleNamespace(
            enabled=True,
            storage_path=str(tmp_path / "videos"),
            max_video_size_mb=500,
            max_storage_gb=100,
            preferred_quality="1080p",
            subtitle_languages=["en"],
            auto_cleanup_enabled=True,
            cleanup_after_days=30,
        )
    )


def _make_response_formatter() -> Any:
    formatter = MagicMock()
    formatter.safe_reply = AsyncMock()
    formatter.send_message_draft = AsyncMock()
    formatter.send_youtube_download_notification = AsyncMock()
    formatter.send_youtube_download_complete_notification = AsyncMock()
    return formatter


def _make_lifecycle() -> Any:
    lifecycle = MagicMock()
    lifecycle.handle_request_dedupe_or_create = AsyncMock(return_value=101)
    lifecycle.create_request = AsyncMock(return_value=101)
    return lifecycle


def _make_request(
    *,
    url_text: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    mode: str = "interactive",
    silent: bool = True,
    request_id_override: int | None = None,
    progress_tracker: Any | None = None,
) -> PlatformExtractionRequest:
    return PlatformExtractionRequest(
        message=MagicMock() if mode == "interactive" else None,
        url_text=url_text,
        normalized_url=url_text,
        correlation_id="cid",
        interaction_id=None,
        silent=silent,
        progress_tracker=progress_tracker,
        request_id_override=request_id_override,
        mode=cast("Any", mode),
    )


def _youtube_repo_kwargs() -> dict[str, MagicMock]:
    return {
        "request_repo": MagicMock(),
        "video_repo": MagicMock(),
    }


def _make_platform_extractor(tmp_path: Path) -> YouTubePlatformExtractor:
    return YouTubePlatformExtractor(
        cfg=_make_cfg(tmp_path),
        db=MagicMock(),
        response_formatter=_make_response_formatter(),
        audit_func=lambda *_args, **_kwargs: None,
        lifecycle=_make_lifecycle(),
        **_youtube_repo_kwargs(),
    )


@pytest.mark.asyncio
async def test_platform_extractor_rejects_invalid_url(tmp_path: Path) -> None:
    extractor = _make_platform_extractor(tmp_path)
    with pytest.raises(ValueError, match="could not extract video ID"):
        await extractor.extract(_make_request(url_text="https://example.com/not-youtube"))


@pytest.mark.asyncio
async def test_platform_extractor_reuses_completed_download(tmp_path: Path) -> None:
    extractor: Any = _make_platform_extractor(tmp_path)
    cached = PlatformExtractionResult(
        platform="youtube",
        request_id=1,
        content_text="cached body",
        content_source="cached",
        detected_lang="en",
        title="Cached",
        metadata={"title": "Cached"},
    )
    extractor._session_service.check_storage_limits = AsyncMock()
    extractor._session_service.prepare = AsyncMock(
        return_value=YouTubeDownloadPreparation(
            req_id=1,
            download_id=None,
            wait_for_existing_download=False,
            cached_result=cached,
        )
    )
    extractor._pipeline.run = AsyncMock()

    result = await extractor.extract(_make_request())

    assert result is cached
    extractor._pipeline.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_platform_extractor_reuses_in_progress_download(tmp_path: Path) -> None:
    extractor: Any = _make_platform_extractor(tmp_path)
    expected = PlatformExtractionResult(
        platform="youtube",
        request_id=42,
        content_text="reused body",
        content_source="cached",
        detected_lang="en",
        title="Title",
        metadata={"title": "Title"},
    )
    extractor._session_service.check_storage_limits = AsyncMock()
    extractor._session_service.prepare = AsyncMock(
        return_value=YouTubeDownloadPreparation(
            req_id=42,
            download_id=555,
            wait_for_existing_download=True,
            cached_result=None,
        )
    )
    extractor._session_service.await_existing_download_completion = AsyncMock(
        return_value={"status": "completed"}
    )
    extractor._session_service.build_reused_download_result = AsyncMock(return_value=expected)

    result = await extractor.extract(_make_request())

    assert result is expected
    extractor._session_service.await_existing_download_completion.assert_awaited_once()
    extractor._session_service.build_reused_download_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_uses_vtt_fallback_when_transcript_api_empty(tmp_path: Path) -> None:
    session_service = MagicMock(spec=YouTubeDownloadSessionService)
    session_service.storage_path = tmp_path / "videos"
    session_service.storage_path.mkdir(parents=True, exist_ok=True)
    session_service.mark_download_started = AsyncMock()
    session_service.persist_success = AsyncMock()
    session_service.handle_failure = AsyncMock()
    session_service.cleanup_partial_download_files = MagicMock()
    feedback_service = MagicMock(spec=YouTubeFeedbackService)
    feedback_service.start = AsyncMock(
        return_value=SimpleNamespace(
            updater=None, typing_ctx=None, completed_stages=[], stage_start=0
        )
    )
    feedback_service.mark_transcript_ready = AsyncMock()
    feedback_service.mark_subtitle_fallback = AsyncMock()
    feedback_service.finalize_success = AsyncMock()
    feedback_service.finalize_error = AsyncMock()
    pipeline: Any = YouTubeDownloadPipeline(
        cfg=_make_cfg(tmp_path),
        audit_func=lambda *_args, **_kwargs: None,
        feedback_service=feedback_service,
        session_service=session_service,
    )
    pipeline._extract_transcript_api = AsyncMock(return_value=("", "", False, "api"))
    pipeline._download_video_sync = MagicMock(
        return_value={
            "title": "Example video",
            "channel": "Channel",
            "channel_id": "ch-1",
            "duration": 123,
            "upload_date": "20260101",
            "view_count": 1000,
            "like_count": 100,
            "resolution": "1080p",
            "file_size": 1024 * 1024,
            "vcodec": "h264",
            "acodec": "aac",
            "format_id": "137+140",
            "subtitle_file_path": str(tmp_path / "captions.en.vtt"),
        }
    )
    pipeline._load_transcript_from_vtt = MagicMock(return_value=("vtt transcript body", "en"))

    result = await pipeline.run(
        request=_make_request(),
        video_id="dQw4w9WgXcQ",
        req_id=500,
        download_id=900,
    )

    assert result.content_source == "vtt"
    assert result.detected_lang == "en"
    assert "vtt transcript body" in result.content_text
    assert result.normalized_document is not None
    assert result.metadata["video_provenance"]["primary_fact_source"] == "transcript"
    assert result.metadata["video_controls"]["max_download_size_mb"] == 500
    session_service.persist_success.assert_awaited_once()
    persisted = session_service.persist_success.await_args.kwargs
    assert persisted["transcript_source"] == "vtt"
    # On full success the downloaded files are kept, never cleaned up.
    session_service.cleanup_partial_download_files.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_cleans_partial_files_after_error(tmp_path: Path) -> None:
    session_service = MagicMock(spec=YouTubeDownloadSessionService)
    session_service.storage_path = tmp_path / "videos"
    session_service.storage_path.mkdir(parents=True, exist_ok=True)
    session_service.mark_download_started = AsyncMock()
    session_service.persist_success = AsyncMock()
    session_service.handle_failure = AsyncMock()
    session_service.cleanup_partial_download_files = MagicMock()
    feedback_service = MagicMock(spec=YouTubeFeedbackService)
    feedback_service.start = AsyncMock(
        return_value=SimpleNamespace(
            updater=None, typing_ctx=None, completed_stages=[], stage_start=0
        )
    )
    feedback_service.mark_transcript_ready = AsyncMock()
    feedback_service.mark_subtitle_fallback = AsyncMock()
    feedback_service.finalize_success = AsyncMock()
    feedback_service.finalize_error = AsyncMock()
    pipeline: Any = YouTubeDownloadPipeline(
        cfg=_make_cfg(tmp_path),
        audit_func=lambda *_args, **_kwargs: None,
        feedback_service=feedback_service,
        session_service=session_service,
    )
    pipeline._extract_transcript_api = AsyncMock(return_value=("body", "en", False, "api"))
    pipeline._download_video_sync = MagicMock(side_effect=ValueError("boom"))

    with pytest.raises(ValueError, match="boom"):
        await pipeline.run(
            request=_make_request(),
            video_id="dQw4w9WgXcQ",
            req_id=500,
            download_id=900,
        )

    session_service.handle_failure.assert_awaited_once()
    session_service.cleanup_partial_download_files.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_cleans_partial_files_when_extraction_fails_after_download(
    tmp_path: Path,
) -> None:
    """Regression: a failure in post-download packaging must still clean up.

    The download + persist succeed, then video_source_extractor.extract raises.
    The request is marked failed and the downloaded files must be removed rather
    than orphaned (previously the success flag was set before extraction, so the
    finally-block cleanup was skipped and the files leaked).
    """
    session_service = MagicMock(spec=YouTubeDownloadSessionService)
    session_service.storage_path = tmp_path / "videos"
    session_service.storage_path.mkdir(parents=True, exist_ok=True)
    session_service.mark_download_started = AsyncMock()
    session_service.persist_success = AsyncMock()
    session_service.handle_failure = AsyncMock()
    session_service.cleanup_partial_download_files = MagicMock()
    feedback_service = MagicMock(spec=YouTubeFeedbackService)
    feedback_service.start = AsyncMock(
        return_value=SimpleNamespace(
            updater=None, typing_ctx=None, completed_stages=[], stage_start=0
        )
    )
    feedback_service.mark_transcript_ready = AsyncMock()
    feedback_service.mark_subtitle_fallback = AsyncMock()
    feedback_service.finalize_success = AsyncMock()
    feedback_service.finalize_error = AsyncMock()
    pipeline: Any = YouTubeDownloadPipeline(
        cfg=_make_cfg(tmp_path),
        audit_func=lambda *_args, **_kwargs: None,
        feedback_service=feedback_service,
        session_service=session_service,
    )
    pipeline._extract_transcript_api = AsyncMock(return_value=("body", "en", False, "api"))
    pipeline._download_video_sync = MagicMock(
        return_value={
            "title": "Example video",
            "channel": "Channel",
            "channel_id": "ch-1",
            "duration": 123,
            "upload_date": "20260101",
            "view_count": 1000,
            "like_count": 100,
            "resolution": "1080p",
            "file_size": 1024 * 1024,
            "vcodec": "h264",
            "acodec": "aac",
            "format_id": "137+140",
            "video_file_path": str(tmp_path / "videos" / "video.mp4"),
            "thumbnail_file_path": str(tmp_path / "videos" / "thumb.jpg"),
        }
    )
    # Download + persist succeed; packaging/extraction then fails.
    pipeline._video_source_extractor = MagicMock()
    pipeline._video_source_extractor.extract = MagicMock(
        side_effect=RuntimeError("packaging failed")
    )

    with pytest.raises(RuntimeError, match="packaging failed"):
        await pipeline.run(
            request=_make_request(),
            video_id="dQw4w9WgXcQ",
            req_id=500,
            download_id=900,
        )

    session_service.persist_success.assert_awaited_once()
    session_service.handle_failure.assert_awaited_once()
    # The fix: partial download files are cleaned up on post-download failure.
    session_service.cleanup_partial_download_files.assert_called_once()


@pytest.mark.asyncio
async def test_session_service_rejects_when_storage_limit_still_exceeded(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.youtube.max_storage_gb = 0.0000001
    cfg.youtube.auto_cleanup_enabled = False
    session: Any = YouTubeDownloadSessionService(
        cfg=cfg,
        db=MagicMock(),
        response_formatter=_make_response_formatter(),
        audit_func=lambda *_args, **_kwargs: None,
        lifecycle=_make_lifecycle(),
        **_youtube_repo_kwargs(),
    )
    session.calculate_storage_usage = MagicMock(return_value=1024 * 1024)

    with pytest.raises(ValueError, match="Storage limit exceeded"):
        await session.check_storage_limits()


@pytest.mark.asyncio
async def test_session_service_uses_request_id_override_in_pure_mode(tmp_path: Path) -> None:
    session: Any = YouTubeDownloadSessionService(
        cfg=_make_cfg(tmp_path),
        db=MagicMock(),
        response_formatter=_make_response_formatter(),
        audit_func=lambda *_args, **_kwargs: None,
        lifecycle=_make_lifecycle(),
        **_youtube_repo_kwargs(),
    )
    session.video_repo.async_get_video_download_by_request = AsyncMock(return_value=None)
    session.video_repo.async_create_video_download = AsyncMock(return_value=901)

    result = await session.prepare(
        request=_make_request(mode="pure", request_id_override=777),
        video_id="dQw4w9WgXcQ",
    )

    assert result.req_id == 777
    session.video_repo.async_create_video_download.assert_awaited_once_with(
        request_id=777,
        video_id="dQw4w9WgXcQ",
        status="pending",
    )


@pytest.mark.asyncio
async def test_session_service_persists_download_paths_for_retention(tmp_path: Path) -> None:
    repos = _youtube_repo_kwargs()
    repos["video_repo"].async_update_video_download = AsyncMock()
    repos["request_repo"].async_update_request_status = AsyncMock()
    repos["request_repo"].async_update_request_lang_detected = AsyncMock()
    session = YouTubeDownloadSessionService(
        cfg=_make_cfg(tmp_path),
        db=MagicMock(),
        response_formatter=_make_response_formatter(),
        audit_func=lambda *_args, **_kwargs: None,
        lifecycle=_make_lifecycle(),
        **repos,
    )
    paths = {
        "video_file_path": str(tmp_path / "video.mp4"),
        "subtitle_file_path": str(tmp_path / "captions.vtt"),
        "metadata_file_path": str(tmp_path / "metadata.json"),
        "thumbnail_file_path": str(tmp_path / "thumb.jpg"),
    }

    await session.persist_success(
        req_id=10,
        download_id=20,
        video_metadata={"title": "Video", **paths},
        transcript_text="body",
        transcript_lang="en",
        auto_generated=False,
        transcript_source="vtt",
        detected_lang="en",
    )

    persisted = repos["video_repo"].async_update_video_download.await_args.kwargs
    assert {key: persisted[key] for key in paths} == paths
    assert persisted["status"] == "completed"
    assert persisted["download_completed_at"] is not None
    repos["video_repo"].async_update_video_download_status.assert_not_called()


@pytest.mark.asyncio
async def test_feedback_service_uses_progress_tracker_when_present(tmp_path: Path) -> None:
    feedback = YouTubeFeedbackService(response_formatter=_make_response_formatter())
    request = _make_request(progress_tracker=MagicMock(), silent=False)

    with patch(
        "app.adapters.youtube.feedback_service.ProgressMessageUpdater",
        autospec=True,
    ) as updater_cls:
        updater = updater_cls.return_value
        updater.start = AsyncMock()
        state = await feedback.start(request=request, video_id="abc123")

    assert state.updater is updater
    updater.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_feedback_service_uses_typing_indicator_without_progress_tracker(
    tmp_path: Path,
) -> None:
    feedback = YouTubeFeedbackService(response_formatter=_make_response_formatter())
    request = _make_request(progress_tracker=None, silent=False)
    typing_ctx = MagicMock()
    typing_ctx.__aenter__ = AsyncMock()
    typing_ctx.__aexit__ = AsyncMock()

    with patch(
        "app.adapters.youtube.feedback_service.typing_indicator",
        return_value=typing_ctx,
    ):
        state = await feedback.start(request=request, video_id="abc123")

    assert state.typing_ctx is typing_ctx
    typing_ctx.__aenter__.assert_awaited_once()
