"""Audio-only media fetcher for URL inputs to /transcribe.

Reuses the ``yt_dlp`` package already pinned by ratatoskr's ``youtube``
extra, but with leaner options than ``app.adapters.youtube`` (which is tuned
for full-quality video downloads + subtitles + metadata). Synchronous; wrap
in ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class MediaFetchError(RuntimeError):
    """Raised when yt-dlp cannot retrieve audio from a URL."""


def _build_audio_only_opts(workdir: Path, max_filesize_mb: int | None) -> dict[str, Any]:
    """Build a yt-dlp options dict that prefers the smallest viable audio stream."""
    opts: dict[str, Any] = {
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        # download_addr / h264 cover TikTok's audio-bearing formats; bestaudio
        # is the right fallback for YouTube etc.; best is a last resort.
        "format": "download_addr/h264/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "prefer_ffmpeg": True,
    }
    if max_filesize_mb and max_filesize_mb > 0:
        opts["max_filesize"] = int(max_filesize_mb) * 1024 * 1024
    return opts


def fetch_url_to_local_sync(
    url: str,
    workdir: Path,
    *,
    max_filesize_mb: int | None = None,
    correlation_id: str | None = None,
) -> Path:
    """Download ``url`` into ``workdir`` and return the resulting media file path.

    Picks the largest downloaded file in ``workdir`` (yt-dlp can leave info-json
    siblings, but those are smaller than the actual media). The caller owns the
    workdir and is responsible for cleanup.
    """
    import yt_dlp

    workdir.mkdir(parents=True, exist_ok=True)
    opts = _build_audio_only_opts(workdir, max_filesize_mb)

    logger.info(
        "transcription_media_fetch_start",
        extra={"url": url, "workdir": str(workdir), "cid": correlation_id},
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        logger.warning(
            "transcription_media_fetch_failed",
            extra={"url": url, "cid": correlation_id, "error": str(exc)},
        )
        msg = f"yt-dlp failed for {url}: {str(exc)[:200]}"
        raise MediaFetchError(msg) from exc
    except Exception as exc:
        logger.warning(
            "transcription_media_fetch_failed",
            extra={"url": url, "cid": correlation_id, "error": str(exc)},
        )
        msg = f"unexpected yt-dlp error for {url}: {str(exc)[:200]}"
        raise MediaFetchError(msg) from exc

    candidates = [p for p in workdir.iterdir() if p.is_file()]
    if not candidates:
        msg = f"yt-dlp produced no files for {url}"
        raise MediaFetchError(msg)
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    chosen = candidates[0]
    logger.info(
        "transcription_media_fetch_complete",
        extra={
            "url": url,
            "file": chosen.name,
            "bytes": chosen.stat().st_size,
            "cid": correlation_id,
        },
    )
    return chosen
