"""Temporary file helpers for summary exports."""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

EXPORT_TEMP_DIRNAME = "ratatoskr-exports"
EXPORT_TEMP_PREFIX = "ratatoskr-export-"
DEFAULT_STALE_EXPORT_MAX_AGE_SECONDS = 24 * 60 * 60


def get_export_temp_dir() -> Path:
    """Return the private directory used for transient export files."""
    path = Path(tempfile.gettempdir()) / EXPORT_TEMP_DIRNAME
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        logger.debug(
            "export_temp_dir_chmod_failed",
            extra={
                "path_ref": _safe_path_ref(path),
                "error_type": type(exc).__name__,
                "error": _safe_os_error(exc),
            },
        )
    return path


@contextmanager
def named_export_temp_file(
    *,
    suffix: str,
    mode: str = "w",
    encoding: str | None = "utf-8",
) -> Iterator[Any]:
    """Create a delete=False temp file in the export temp directory."""
    export_dir = get_export_temp_dir()
    if "b" not in mode:
        temp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode=mode,
            suffix=suffix,
            prefix=EXPORT_TEMP_PREFIX,
            dir=export_dir,
            delete=False,
            encoding=encoding,
        )
    else:
        temp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode=mode,
            suffix=suffix,
            prefix=EXPORT_TEMP_PREFIX,
            dir=export_dir,
            delete=False,
        )
    try:
        yield temp_file
    finally:
        temp_file.close()


def cleanup_export_file(path: str | os.PathLike[str]) -> None:
    """Remove a generated export file, logging cleanup errors without exposing full paths."""
    export_path = Path(path)
    try:
        export_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            "export_temp_file_cleanup_failed",
            extra={
                "path_ref": _safe_path_ref(export_path),
                "suffix": export_path.suffix,
                "error_type": type(exc).__name__,
                "error": _safe_os_error(exc),
            },
        )


def cleanup_stale_export_files(
    *,
    max_age_seconds: int = DEFAULT_STALE_EXPORT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> dict[str, int]:
    """Delete old export temp files left behind by crashed workers or interrupted responses."""
    export_dir = get_export_temp_dir()
    cutoff = (time.time() if now is None else now) - max_age_seconds
    result = {"deleted": 0, "failed": 0, "skipped": 0}

    for path in export_dir.glob(f"{EXPORT_TEMP_PREFIX}*"):
        if not path.is_file():
            result["skipped"] += 1
            continue
        try:
            if path.stat().st_mtime > cutoff:
                result["skipped"] += 1
                continue
            path.unlink()
            result["deleted"] += 1
        except OSError as exc:
            result["failed"] += 1
            logger.warning(
                "stale_export_temp_file_cleanup_failed",
                extra={
                    "path_ref": _safe_path_ref(path),
                    "suffix": path.suffix,
                    "error_type": type(exc).__name__,
                    "error": _safe_os_error(exc),
                },
            )

    if result["deleted"] or result["failed"]:
        logger.info("stale_export_temp_files_cleaned", extra=result)
    return result


def _safe_path_ref(path: Path) -> str:
    return sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:12]


def _safe_os_error(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__
