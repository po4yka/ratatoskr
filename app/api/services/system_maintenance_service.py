"""Service layer for system maintenance operations."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.api.exceptions import ProcessingError
from app.config.settings import load_config
from app.core.logging_utils import get_logger
from app.db.models import ALL_MODELS
from app.infrastructure.cache.redis_cache import RedisCache

if TYPE_CHECKING:
    from pathlib import Path

    from app.db.session import Database

logger = get_logger(__name__)


_DB_INFO_TABLE_ALLOWLIST: frozenset[str] = frozenset(model.__tablename__ for model in ALL_MODELS)


@dataclass(frozen=True)
class DatabaseDumpFile:
    """Metadata for a generated PostgreSQL backup file."""

    path: str
    filename: str
    media_type: str = "application/octet-stream"


def _silent_unlink(path: str) -> None:
    """Remove a file, suppressing any OS-level error."""
    try:
        os.unlink(path)
    except OSError:
        pass


class SystemMaintenanceService:
    """Orchestrates DB/Redis maintenance tasks for API endpoints."""

    def __init__(
        self,
        *,
        database: Database | None = None,
        backup_dir: str | None = None,
    ) -> None:
        if database is None:
            from app.api.dependencies.database import get_session_manager

            database = get_session_manager()
        self._database = database
        self._backup_dir = backup_dir or tempfile.gettempdir()

    def build_db_dump_file(
        self,
        *,
        user_id: int,
    ) -> DatabaseDumpFile:
        """Create a unique per-request DB backup with owner-only permissions.

        Each call creates a new temp file; callers are responsible for deleting it.
        """
        fd, unique_path = tempfile.mkstemp(
            prefix="ratatoskr_dump_",
            suffix=".dump",
            dir=self._backup_dir,
        )
        os.close(fd)

        try:
            self._create_backup(backup_path=unique_path, user_id=user_id)
            os.chmod(unique_path, 0o600)
        except Exception:
            _silent_unlink(unique_path)
            raise

        mtime = os.path.getmtime(unique_path)
        timestamp = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y%m%d_%H%M%S")
        return DatabaseDumpFile(path=unique_path, filename=f"ratatoskr_backup_{timestamp}.dump")

    async def get_db_info(self) -> dict[str, object]:
        """Return PostgreSQL metadata and allowlisted table row counts."""
        table_counts: dict[str, int] = {}
        database_size_mb = 0.0

        try:
            overview = await self._database.inspection.async_get_database_overview()
            tables = overview.get("tables", {})
            if isinstance(tables, dict):
                table_counts = {
                    table: int(count)
                    for table, count in tables.items()
                    if table in _DB_INFO_TABLE_ALLOWLIST
                }
            database_size_mb = await self._database.inspection.async_database_size_mb()
        except Exception as exc:
            logger.error("db_info_failed", extra={"error": str(exc)})
            table_counts["__error__"] = -1

        return {
            "file_size_mb": database_size_mb,
            "database_size_mb": database_size_mb,
            "table_counts": table_counts,
            "db_path": "postgresql",
        }

    async def clear_url_cache(self) -> int:
        """Clear URL cache entries from Redis."""
        cfg = load_config(allow_stub_telegram=True)
        cache = RedisCache(cfg)

        try:
            return await cache.clear_prefix("url")
        except Exception as exc:
            logger.error("clear_cache_failed", extra={"error": str(exc)})
            raise ProcessingError(f"Cache clear failed: {exc}") from exc

    def _create_backup(self, *, backup_path: str, user_id: int) -> None:
        temp_backup_path = backup_path + ".tmp"

        try:
            if os.path.exists(temp_backup_path):
                os.remove(temp_backup_path)

            created_path: Path = self._database.create_backup_copy(temp_backup_path)
            os.replace(created_path, backup_path)
            logger.info(
                "database_backup_created_for_api",
                extra={"backup_path": backup_path, "user_id": user_id},
            )
        except Exception as exc:
            cleanup_error: str | None = None

            if os.path.exists(temp_backup_path):
                try:
                    os.remove(temp_backup_path)
                except OSError as cleanup_exc:
                    cleanup_error = str(cleanup_exc)
                    logger.debug("temp_backup_cleanup_failed", extra={"error": cleanup_error})

            details = f"Backup failed: {exc!s}"
            if cleanup_error:
                details += f" (temporary file cleanup also failed: {cleanup_error})"
            raise ProcessingError(details) from exc
