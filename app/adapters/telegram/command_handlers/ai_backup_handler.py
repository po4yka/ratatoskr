"""Telegram command handlers for /ai_backup and /ai_backups.

/ai_backups  -- list the per-service backup status rows for the operator.
/ai_backup   -- short overview of the AI account-backup subsystem state.

Read-only in this phase: the authenticated scrape is not functional yet, so
these commands surface status and configuration only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.ai_backup.repository import AiBackupRepository
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)


def _format_backup_row(row: object) -> str:
    """Format one AiAccountBackup row for display."""
    service = getattr(getattr(row, "service", None), "value", None) or "?"
    status = getattr(getattr(row, "status", None), "value", None) or "?"
    last = getattr(row, "last_backed_up_at", None)
    last_str = last.strftime("%Y-%m-%d %H:%M UTC") if last else "never"
    error = getattr(row, "last_error", None)
    suffix = f"\n  last_error: {error}" if error else ""
    return f"[{service}] status={status}  last_backup={last_str}{suffix}"


class AiBackupHandler(HandlerDependenciesMixin):
    """Handle /ai_backup and /ai_backups commands.

    ``ai_backup_repo_factory`` is a zero-argument callable injected by the DI
    layer that returns an ``AiBackupRepository``. Keeping the factory outside
    this module avoids a runtime cross-adapter import from ``telegram`` into
    ``ai_backup``.
    """

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        response_formatter: ResponseFormatter,
        ai_backup_repo_factory: Callable[[], AiBackupRepository] | None = None,
    ) -> None:
        super().__init__(cfg=cfg, db=db, response_formatter=response_formatter)
        self._repo_factory = ai_backup_repo_factory
        self._enabled = cfg.ai_backup.enabled
        self._chatgpt_enabled = cfg.ai_backup.chatgpt_enabled
        self._claude_enabled = cfg.ai_backup.claude_enabled

    @property
    def _repo(self) -> AiBackupRepository:
        if self._repo_factory is None:
            raise RuntimeError(
                "AiBackupHandler requires an ai_backup_repo_factory; "
                "wire it up in the DI layer (app/di/telegram_commands.py)."
            )
        return self._repo_factory()

    @combined_handler("command_ai_backup", "ai_backup")
    async def handle_ai_backup(self, ctx: CommandExecutionContext) -> None:
        """Handle /ai_backup -- short subsystem overview."""
        if not self._enabled:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "AI account backup is disabled. Set AI_BACKUP_ENABLED=true (plus a "
                "per-service flag) to enable it. Use /ai_backups to view status.",
            )
            return

        services = []
        if self._chatgpt_enabled:
            services.append("chatgpt")
        if self._claude_enabled:
            services.append("claude")
        services_str = ", ".join(services) if services else "none"
        await ctx.response_formatter.safe_reply(
            ctx.message,
            f"AI account backup is enabled. Services: {services_str}.\n"
            "Use /ai_backups to view per-service status.",
        )

    @combined_handler("command_ai_backups", "ai_backups")
    async def handle_ai_backups(self, ctx: CommandExecutionContext) -> None:
        """Handle /ai_backups -- list the operator's per-service backup status."""
        rows = await self._repo.list_for_user(ctx.uid)

        if not rows:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No AI account backups tracked yet.\n"
                "They appear after the first scheduled run with AI_BACKUP_ENABLED=true.",
            )
            return

        lines = [_format_backup_row(r) for r in rows]
        text = f"AI account backups ({len(rows)}):\n\n" + "\n\n".join(lines)
        await ctx.response_formatter.safe_reply(ctx.message, text)
