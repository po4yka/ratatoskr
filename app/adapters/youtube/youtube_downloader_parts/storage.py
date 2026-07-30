from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

ELIGIBLE_SUFFIXES = {".mp4", ".info.json", ".vtt", ".jpg", ".png", ".webp"}


def _has_eligible_suffix(path: Path, eligible_suffixes: set[str]) -> bool:
    """Match on the filename tail, not Path.suffix.

    ``Path("v.info.json").suffix`` is ``".json"``, so ``.info.json`` could never
    match: yt-dlp's metadata sidecars were counted in neither the usage total nor
    the cleanup candidates, and accumulated on the SD card forever even though
    metadata_file_path is persisted for them.
    """
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in eligible_suffixes)


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def calculate_storage_usage(
    storage_path: Path, *, eligible_suffixes: set[str] = ELIGIBLE_SUFFIXES
) -> int:
    total = 0
    try:
        for file_path in storage_path.rglob("*"):
            if file_path.is_file() and _has_eligible_suffix(file_path, eligible_suffixes):
                total += file_path.stat().st_size
    except Exception as exc:
        logger.warning("youtube_storage_calculation_failed", extra={"error": str(exc)})
        return total
    return total


def auto_cleanup_storage(
    storage_path: Path,
    *,
    current_usage: int,
    max_storage: int,
    retention_days: int,
    eligible_suffixes: set[str] = ELIGIBLE_SUFFIXES,
    now: datetime | None = None,
) -> int:
    """Remove old files until under budget or no candidates remain. Returns reclaimed bytes."""
    reclaimed = 0
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)

    candidates: list[tuple[Path, int, float]] = []
    for file_path in storage_path.rglob("*"):
        if not file_path.is_file():
            continue
        if not _has_eligible_suffix(file_path, eligible_suffixes):
            continue
        try:
            stat = file_path.stat()
        except OSError:
            logger.debug(
                "youtube_cleanup_stat_failed", extra={"path": str(file_path)}, exc_info=True
            )
            continue
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        if modified < cutoff:
            candidates.append((file_path, stat.st_size, stat.st_mtime))

    candidates.sort(key=lambda x: x[2])  # oldest first

    for path, size, _ in candidates:
        if current_usage - reclaimed <= max_storage * 0.9:
            break
        try:
            path.unlink()
            reclaimed += size
        except OSError as exc:
            logger.warning(
                "youtube_cleanup_delete_failed",
                extra={"path": str(path), "error": str(exc)},
            )
            continue

    logger.info(
        "youtube_cleanup_completed",
        extra={
            "candidates": len(candidates),
            "reclaimed_bytes": reclaimed,
            "retention_days": retention_days,
        },
    )
    return reclaimed


def cleanup_partial_download_files(
    *,
    output_dir: Path,
    video_id: str,
    paths: Iterable[Path] | None = None,
) -> int:
    """Remove files matching `f\"{video_id}_*\"` under output_dir. Returns deleted count."""
    deleted_count = 0
    if paths is None:
        paths = output_dir.glob(f"{video_id}_*")

    for partial in paths:
        try:
            partial.unlink()
            deleted_count += 1
        except OSError:
            logger.debug(
                "youtube_partial_cleanup_unlink_failed",
                extra={"path": str(partial)},
                exc_info=True,
            )
            continue

    # Remove empty date directory
    try:
        if not any(output_dir.iterdir()):
            output_dir.rmdir()
    except OSError:
        logger.debug(
            "youtube_partial_cleanup_rmdir_failed",
            extra={"path": str(output_dir)},
            exc_info=True,
        )

    return deleted_count
