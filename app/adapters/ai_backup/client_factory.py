"""Factory that selects the backup client for a service."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - used in a runtime function signature
from typing import TYPE_CHECKING

from app.adapters.ai_backup.chatgpt_client import ChatGptClient
from app.adapters.ai_backup.claude_client import ClaudeClient
from app.db.models.ai_backup import AiBackupService

if TYPE_CHECKING:
    from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter
    from app.adapters.content.browser_auth.authenticated_context import AuthedFetcher
    from app.config.ai_backup import AiBackupConfig


def build_client(
    service: AiBackupService,
    fetcher: AuthedFetcher,
    writer: AiBackupDiskWriter,
    cfg: AiBackupConfig,
    *,
    last_backed_up_at: dt.datetime | None,
) -> ChatGptClient | ClaudeClient:
    """Return the configured client for ``service``."""
    if service == AiBackupService.CHATGPT:
        return ChatGptClient(
            fetcher,
            writer,
            download_files=cfg.download_files,
            incremental=cfg.incremental,
            last_backed_up_at=last_backed_up_at,
        )
    if service == AiBackupService.CLAUDE:
        return ClaudeClient(
            fetcher,
            writer,
            download_files=cfg.download_files,
            incremental=cfg.incremental,
            last_backed_up_at=last_backed_up_at,
        )
    raise ValueError(f"Unknown AiBackupService: {service!r}")


__all__ = ["build_client"]
