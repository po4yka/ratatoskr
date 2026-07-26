"""Session and persistence lifecycle for YouTube platform extraction."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.adapters.content.platform_extraction.models import (
    PlatformExtractionRequest,
    PlatformExtractionResult,
)
from app.adapters.video.source_extractor import (
    MetadataDrivenVideoSourceExtractor,
    VideoSourceRequest,
    build_video_controls_from_config,
)
from app.adapters.youtube.youtube_downloader_parts import metadata as _metadata, storage as _storage
from app.application.dto.aggregation import (
    SourceMediaAsset,
    SourceMediaKind,
)
from app.core.async_utils import raise_if_cancelled
from app.core.lang import detect_language
from app.core.logging_utils import get_logger
from app.core.urls.normalization import url_hash_sha256
from app.core.urls.youtube import extract_youtube_video_id
from app.domain.models.request import RequestStatus
from app.domain.models.source import SourceItem, SourceKind

if TYPE_CHECKING:
    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.application.ports.requests import RequestRepositoryPort, VideoDownloadRepositoryPort

logger = get_logger(__name__)


@dataclass(slots=True)
class YouTubeDownloadPreparation:
    req_id: int
    download_id: int | None
    wait_for_existing_download: bool
    cached_result: PlatformExtractionResult | None


class YouTubeDownloadSessionService:
    """Own request/download rows, storage limits, reuse, and persistence."""

    def __init__(
        self,
        *,
        cfg: Any,
        db: Any,
        response_formatter: ResponseFormatter,
        audit_func: Any,
        lifecycle: PlatformRequestLifecycle,
        request_repo: RequestRepositoryPort | None = None,
        video_repo: VideoDownloadRepositoryPort | None = None,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._response_formatter = response_formatter
        self._audit = audit_func
        self._lifecycle = lifecycle
        if request_repo is None:
            msg = "request_repo must be provided by the composition layer"
            raise ValueError(msg)
        if video_repo is None:
            msg = "video_repo must be provided by the composition layer"
            raise ValueError(msg)
        self.request_repo = request_repo
        self.video_repo = video_repo
        self.storage_path = Path(cfg.youtube.storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self._url_locks: dict[str, asyncio.Lock] = {}
        self._video_source_extractor = MetadataDrivenVideoSourceExtractor()

    async def check_storage_limits(self) -> None:
        current_usage = self.calculate_storage_usage()
        max_storage = self._cfg.youtube.max_storage_gb * 1024 * 1024 * 1024
        threshold = max_storage * 0.9

        if current_usage > threshold and self._cfg.youtube.auto_cleanup_enabled:
            reclaimed = await asyncio.to_thread(
                self.auto_cleanup_storage,
                current_usage,
                max_storage,
            )
            current_usage = self.calculate_storage_usage()
            logger.info(
                "youtube_storage_cleanup_attempted",
                extra={
                    "current_gb": current_usage / 1024 / 1024 / 1024,
                    "max_gb": self._cfg.youtube.max_storage_gb,
                    "reclaimed_gb": reclaimed / 1024 / 1024 / 1024,
                },
            )

        if current_usage > max_storage:
            raise ValueError(
                "❌ Storage limit exceeded. Unable to download new videos until cleanup frees space."
            )

    def calculate_storage_usage(self) -> int:
        return _storage.calculate_storage_usage(self.storage_path)

    def auto_cleanup_storage(self, current_usage: int, max_storage: int) -> int:
        return _storage.auto_cleanup_storage(
            self.storage_path,
            current_usage=current_usage,
            max_storage=max_storage,
            retention_days=self._cfg.youtube.cleanup_after_days,
            now=datetime.now(UTC),
        )

    async def prepare(
        self,
        *,
        request: PlatformExtractionRequest,
        video_id: str,
    ) -> YouTubeDownloadPreparation:
        dedupe = url_hash_sha256(request.normalized_url)
        url_lock = self._url_locks.setdefault(dedupe, asyncio.Lock())
        async with url_lock:
            req_id = await self._resolve_request_id(
                request=request,
                video_id=video_id,
                dedupe=dedupe,
            )
            existing_download = await self.video_repo.async_get_video_download_by_request(req_id)
            if existing_download and existing_download.get("status") == "completed":
                logger.info(
                    "youtube_video_already_downloaded",
                    extra={
                        "video_id": video_id,
                        "request_id": req_id,
                        "cid": request.correlation_id,
                    },
                )
                return YouTubeDownloadPreparation(
                    req_id=req_id,
                    download_id=None,
                    wait_for_existing_download=False,
                    cached_result=await self.build_reused_download_result(
                        request=request,
                        req_id=req_id,
                        download=existing_download,
                        reuse_message=(
                            "♻️ Reusing previously downloaded video and transcript. "
                            "Skipping re-download."
                        ),
                        warning_key="youtube_cached_reply_failed",
                        missing_transcript_error=(
                            "❌ Cached video found but no transcript or subtitles were available. "
                            "Try re-downloading with subtitles enabled."
                        ),
                    ),
                )

            if existing_download and existing_download.get("status") in {"pending", "downloading"}:
                logger.info(
                    "youtube_download_in_progress_reuse",
                    extra={
                        "video_id": video_id,
                        "request_id": req_id,
                        "download_id": existing_download.get("id"),
                        "status": existing_download.get("status"),
                        "cid": request.correlation_id,
                    },
                )
                return YouTubeDownloadPreparation(
                    req_id=req_id,
                    download_id=existing_download.get("id"),
                    wait_for_existing_download=True,
                    cached_result=None,
                )

            download_id = await self.video_repo.async_create_video_download(
                request_id=req_id,
                video_id=video_id,
                status="pending",
            )
            return YouTubeDownloadPreparation(
                req_id=req_id,
                download_id=download_id,
                wait_for_existing_download=False,
                cached_result=None,
            )

    async def await_existing_download_completion(
        self,
        *,
        req_id: int,
        correlation_id: str | None,
        timeout_sec: float = 620.0,
        poll_interval_sec: float = 1.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            existing_download = await self.video_repo.async_get_video_download_by_request(req_id)
            if not isinstance(existing_download, dict):
                await asyncio.sleep(poll_interval_sec)
                continue

            status = str(existing_download.get("status") or "").lower()
            if status == "completed":
                return existing_download
            if status == "error":
                error_text = existing_download.get("error_text") or "YouTube download failed"
                raise ValueError(f"❌ {error_text}")

            await asyncio.sleep(poll_interval_sec)

        raise TimeoutError(
            "Timed out waiting for an existing YouTube download to finish. Please try again."
        )

    async def build_reused_download_result(
        self,
        *,
        request: PlatformExtractionRequest,
        req_id: int,
        download: dict[str, Any],
        reuse_message: str,
        warning_key: str,
        missing_transcript_error: str,
    ) -> PlatformExtractionResult:
        if request.mode == "interactive" and not request.silent and request.message is not None:
            try:
                await self._response_formatter.safe_reply(request.message, reuse_message)
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.warning(warning_key, exc_info=True)

        metadata = self.build_metadata_dict(download)
        transcript_value = download.get("transcript_text")
        transcript_text = (
            transcript_value if isinstance(transcript_value, str) else str(transcript_value or "")
        )
        if not transcript_text.strip():
            raise ValueError(missing_transcript_error)

        transcript_source = download.get("transcript_source") or "cached"
        detected_lang = download.get("subtitle_language") or detect_language(transcript_text)
        source_item = SourceItem.create(
            kind=SourceKind.YOUTUBE_VIDEO,
            original_value=request.url_text,
            normalized_value=request.normalized_url,
            external_id=str(metadata.get("video_id") or ""),
            request_id=req_id,
            title_hint=metadata.get("title"),
            metadata={"platform": "youtube"},
        )
        existing_media: list[SourceMediaAsset] = []
        video_file_path = str(download.get("video_file_path") or "").strip()
        if video_file_path:
            existing_media.append(
                SourceMediaAsset(
                    kind=SourceMediaKind.VIDEO,
                    local_path=video_file_path,
                    duration_sec=float(metadata.get("duration") or 0) or None,
                    metadata={"platform": "youtube"},
                )
            )
        thumbnail_file_path = str(download.get("thumbnail_file_path") or "").strip()
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
                title=str(metadata.get("title") or "").strip() or None,
                body_text=_metadata.format_metadata_header(metadata),
                transcript_text=transcript_text,
                transcript_source=str(transcript_source),
                content_source=str(transcript_source),
                content_text_override=_metadata.combine_metadata_and_transcript(
                    metadata, transcript_text
                ),
                detected_language=str(detected_lang),
                primary_video_url=None,
                poster_image_urls=(),
                existing_media=tuple(existing_media),
                duration_sec=float(metadata.get("duration") or 0) or None,
                metadata=metadata,
                controls=build_video_controls_from_config(self._cfg),
            )
        )
        return PlatformExtractionResult(
            platform="youtube",
            request_id=req_id,
            content_text=result.content_text,
            content_source=result.content_source,
            detected_lang=result.normalized_document.detected_language or str(detected_lang),
            title=metadata.get("title"),
            images=result.images,
            metadata=result.metadata,
            source_item=source_item,
            normalized_document=result.normalized_document,
        )

    async def mark_download_started(self, download_id: int) -> None:
        await self.video_repo.async_update_video_download_status(
            download_id,
            "downloading",
            download_started_at=datetime.now(UTC),
        )

    async def persist_success(
        self,
        *,
        req_id: int,
        download_id: int,
        video_metadata: dict[str, Any],
        transcript_text: str,
        transcript_lang: str,
        auto_generated: bool,
        transcript_source: str,
        detected_lang: str,
    ) -> None:
        await self.video_repo.async_update_video_download(
            download_id,
            title=video_metadata.get("title"),
            channel=video_metadata.get("channel"),
            channel_id=video_metadata.get("channel_id"),
            duration_sec=video_metadata.get("duration"),
            upload_date=video_metadata.get("upload_date"),
            view_count=video_metadata.get("view_count"),
            like_count=video_metadata.get("like_count"),
            resolution=video_metadata.get("resolution"),
            file_size_bytes=video_metadata.get("file_size"),
            video_codec=video_metadata.get("vcodec"),
            audio_codec=video_metadata.get("acodec"),
            format_id=video_metadata.get("format_id"),
            video_file_path=video_metadata.get("video_file_path"),
            subtitle_file_path=video_metadata.get("subtitle_file_path"),
            metadata_file_path=video_metadata.get("metadata_file_path"),
            thumbnail_file_path=video_metadata.get("thumbnail_file_path"),
            transcript_text=transcript_text,
            subtitle_language=transcript_lang,
            auto_generated=auto_generated,
            transcript_source=transcript_source,
            status="completed",
            download_completed_at=datetime.now(UTC),
        )
        await self.request_repo.async_update_request_status(req_id, RequestStatus.COMPLETED)
        await self.request_repo.async_update_request_lang_detected(req_id, detected_lang)

    async def handle_failure(
        self,
        *,
        req_id: int,
        download_id: int,
        video_id: str,
        error: Exception,
        correlation_id: str | None,
    ) -> None:
        await self.video_repo.async_update_video_download_status(
            download_id,
            "error",
            error_text=str(error),
        )
        await self.request_repo.async_update_request_status(req_id, RequestStatus.ERROR)
        self._audit(
            "ERROR",
            "youtube_download_failed",
            {
                "video_id": video_id,
                "request_id": req_id,
                "error": str(error),
                "cid": correlation_id,
            },
        )
        logger.error(
            "youtube_download_failed",
            extra={"video_id": video_id, "error": str(error), "cid": correlation_id},
        )

    def cleanup_partial_download_files(
        self,
        *,
        output_dir: Path,
        video_id: str,
        correlation_id: str | None,
    ) -> None:
        try:
            if output_dir.exists():
                deleted_count = _storage.cleanup_partial_download_files(
                    output_dir=output_dir,
                    video_id=video_id,
                )
                if deleted_count > 0:
                    logger.info(
                        "youtube_partial_download_cleaned",
                        extra={
                            "video_id": video_id,
                            "cid": correlation_id,
                            "files_removed": deleted_count,
                        },
                    )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning("youtube_partial_cleanup_failed", exc_info=True)

    def build_metadata_dict(self, download: dict[str, Any] | Any) -> dict[str, Any]:
        if isinstance(download, dict):
            return {
                "video_id": download.get("video_id"),
                "title": download.get("title"),
                "channel": download.get("channel"),
                "channel_id": download.get("channel_id"),
                "duration": download.get("duration_sec"),
                "resolution": download.get("resolution"),
                "file_size": download.get("file_size_bytes"),
                "upload_date": download.get("upload_date"),
                "view_count": download.get("view_count"),
                "like_count": download.get("like_count"),
                "video_file_path": download.get("video_file_path"),
                "subtitle_file_path": download.get("subtitle_file_path"),
                "thumbnail_file_path": download.get("thumbnail_file_path"),
            }
        return {
            "video_id": download.video_id,
            "title": download.title,
            "channel": download.channel,
            "channel_id": download.channel_id,
            "duration": download.duration_sec,
            "resolution": download.resolution,
            "file_size": download.file_size_bytes,
            "upload_date": download.upload_date,
            "view_count": download.view_count,
            "like_count": download.like_count,
            "video_file_path": download.video_file_path,
            "subtitle_file_path": download.subtitle_file_path,
            "thumbnail_file_path": download.thumbnail_file_path,
        }

    async def _resolve_request_id(
        self,
        *,
        request: PlatformExtractionRequest,
        video_id: str,
        dedupe: str,
    ) -> int:
        if request.request_id_override is not None:
            return request.request_id_override

        existing_req = await self.request_repo.async_get_request_by_dedupe_hash(dedupe)
        if isinstance(existing_req, Mapping):
            req_id = int(existing_req["id"])
            logger.info(
                "youtube_dedupe_hit",
                extra={"video_id": video_id, "request_id": req_id, "cid": request.correlation_id},
            )
            return req_id

        return await self._create_video_request(request=request, dedupe=dedupe)

    async def _create_video_request(
        self,
        *,
        request: PlatformExtractionRequest,
        dedupe: str,
    ) -> int:
        if request.mode == "interactive" and request.message is not None:
            return await self._lifecycle.handle_request_dedupe_or_create(
                request,
                dedupe_hash=dedupe,
            )

        request_id = await self._lifecycle.create_request(
            request=request,
            dedupe_hash=dedupe,
        )
        logger.info(
            "youtube_request_created",
            extra={
                "request_id": request_id,
                "url": request.url_text,
                "cid": request.correlation_id,
                "video_id": extract_youtube_video_id(request.url_text),
            },
        )
        return request_id
