"""Backup management command handlers (/backup, /backups).

Lets users create and list backups via Telegram commands.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.infrastructure.persistence.backup_archive_service import async_create_backup_archive

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)

_MAX_BACKUPS_PER_HOUR = 3
_MAX_LIST_COUNT = 5


class BackupHandler(HandlerDependenciesMixin):
    """Handle /backup and /backups commands."""

    def __init__(
        self,
        cfg: Any,
        db: Any,
        response_formatter: Any,
        *,
        backup_repo_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(cfg, db, response_formatter)
        self._backup_repo_factory = backup_repo_factory

    @property
    def _backup_repo(self) -> Any:
        if self._backup_repo_factory is None:
            msg = "Backup repository factory is not configured"
            raise RuntimeError(msg)
        return self._backup_repo_factory()

    @combined_handler("command_backup", "backup")
    async def handle_backup(self, ctx: CommandExecutionContext) -> None:
        """Handle /backup -- create a backup and send it as a document."""
        user_id = ctx.uid

        # Rate limit check
        recent_count = await self._backup_repo.async_count_recent_backups(user_id, since_hours=1)
        if recent_count >= _MAX_BACKUPS_PER_HOUR:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Rate limit: maximum {_MAX_BACKUPS_PER_HOUR} backups per hour. "
                "Please try again later.",
            )
            return

        # Create backup record
        backup = await self._backup_repo.async_create_backup(user_id, type="manual")

        await ctx.response_formatter.safe_reply(
            ctx.message,
            "Creating backup... This may take a moment.",
        )

        try:
            await async_create_backup_archive(
                user_id=user_id, backup_id=int(backup["id"]), db=self._db
            )

            # Reload to get updated fields
            backup = await self._backup_repo.async_get_backup(int(backup["id"]))
            if backup is None:
                raise RuntimeError("Backup record not found after archive creation")

            if backup.get("status") != "completed" or not backup.get("file_path"):
                error_msg = backup.get("error") or "Unknown error"
                await ctx.response_formatter.safe_reply(
                    ctx.message,
                    f"Backup failed: {error_msg}",
                )
                return

            # Send the ZIP file as a document
            file_size_mb = (int(backup.get("file_size_bytes") or 0)) / (1024 * 1024)
            caption = f"Backup completed\nItems: {backup.get('items_count') or 0}\nSize: {file_size_mb:.1f} MB"
            await ctx.message.reply_document(
                document=str(backup["file_path"]),
                caption=caption,
            )

        except Exception as exc:
            logger.exception(
                "telegram_backup_failed",
                extra={"uid": user_id, "backup_id": backup.get("id"), "error": str(exc)},
            )
            # Mark as failed if not already
            try:
                await self._backup_repo.async_update_backup(
                    int(backup["id"]),
                    status="failed",
                    error=str(exc)[:1000],
                )
            except Exception:
                logger.debug(
                    "backup_status_update_failed",
                    extra={"backup_id": backup.get("id")},
                    exc_info=True,
                )
            await ctx.response_formatter.safe_reply(
                ctx.message,
                f"Backup failed: {exc}",
            )

    @combined_handler("command_backups", "backups")
    async def handle_backups(self, ctx: CommandExecutionContext) -> None:
        """Handle /backups -- list recent backups."""
        user_id = ctx.uid

        backups = (await self._backup_repo.async_list_backups(user_id))[:_MAX_LIST_COUNT]

        if not backups:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No backups yet. Use /backup to create one.",
            )
            return

        lines: list[str] = ["Your recent backups:"]
        for i, b in enumerate(backups, 1):
            size_str = _format_size(b.get("file_size_bytes"))
            age_str = _format_age(b.get("created_at"))
            lines.append(f"{i}. {b.get('type')} - {size_str} - {b.get('status')} - {age_str}")

        await ctx.response_formatter.safe_reply(
            ctx.message,
            "\n".join(lines),
        )


def _format_size(bytes_val: int | None) -> str:
    """Format file size for display."""
    if bytes_val is None:
        return "-"
    if bytes_val < 1024:
        return f"{bytes_val} B"
    if bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val / (1024 * 1024):.1f} MB"


def _format_age(dt: datetime | None) -> str:
    """Format a datetime as a human-readable relative age."""
    if dt is None:
        return "unknown"
    now = datetime.now(UTC)
    # Handle naive datetimes from the DB
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    return f"{days}d ago"
