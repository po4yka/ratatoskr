"""Export command handler (/export).

Lets users export all their summaries as JSON, CSV, or HTML file.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.core.logging_utils import get_logger
from app.domain.services.import_export.export_serializers import (
    CsvExporter,
    JsonExporter,
    NetscapeHtmlExporter,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)

_VALID_FORMATS = {"json", "csv", "html"}
_DEFAULT_FORMAT = "json"

_MIME_TYPES = {
    "json": "application/json",
    "csv": "text/csv",
    "html": "text/html",
}

_FILE_EXTENSIONS = {
    "json": "json",
    "csv": "csv",
    "html": "html",
}


class ExportHandler(HandlerDependenciesMixin):
    """Handle /export command."""

    def __init__(
        self,
        cfg: Any,
        db: Any,
        response_formatter: Any,
        *,
        user_content_repo_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(cfg, db, response_formatter)
        self._user_content_repo_factory = user_content_repo_factory

    @combined_handler("command_export", "export", include_text=True)
    async def handle_export(self, ctx: CommandExecutionContext) -> None:
        """Handle /export [json|csv|html] -- export all summaries as a file."""
        fmt = _parse_format(ctx.text)

        if self._user_content_repo_factory is None:
            msg = "User content repository factory is not configured"
            raise RuntimeError(msg)
        repo = self._user_content_repo_factory()
        summaries = await repo.async_export_summaries(
            user_id=ctx.uid,
            tag=None,
            collection_id=None,
        )

        if not summaries:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "No summaries to export.",
            )
            return

        summary_dicts = [_summary_to_dict(s) for s in summaries]

        if fmt == "json":
            content = JsonExporter.serialize(summary_dicts)
        elif fmt == "csv":
            content = CsvExporter.serialize(summary_dicts)
        else:
            content = NetscapeHtmlExporter.serialize(summary_dicts)

        filename = f"summaries.{_FILE_EXTENSIONS[fmt]}"
        file_bytes = content.encode("utf-8")
        buf = io.BytesIO(file_bytes)
        buf.name = filename

        error_occurred = False
        try:
            await ctx.message.reply_document(
                document=buf,
                file_name=filename,
            )
        except Exception as exc:
            error_occurred = True
            logger.exception(
                "export_send_failed",
                extra={"uid": ctx.uid, "format": fmt, "error": str(exc)},
            )
        if error_occurred:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Failed to send the export file. Please try again.",
            )


def _parse_format(text: str) -> str:
    """Extract format argument from command text, defaulting to json."""
    parts = text.strip().split()
    if len(parts) >= 2:
        candidate = parts[1].lower()
        if candidate in _VALID_FORMATS:
            return candidate
    return _DEFAULT_FORMAT


def _summary_to_dict(summary: dict) -> dict:
    """Convert an exported summary row to the serializer payload shape."""
    payload = summary.get("json_payload") if isinstance(summary.get("json_payload"), dict) else {}
    tags = [str(tag.get("name")) for tag in summary.get("tags", []) if isinstance(tag, dict)]
    return {
        "url": summary.get("url", ""),
        "title": payload.get("title", "Untitled"),
        "tags": tags,
        "language": payload.get("language", ""),
        "created_at": str(summary.get("created_at") or ""),
        "is_read": bool(summary.get("is_read", False)),
        "is_favorited": bool(summary.get("is_favorited", False)),
        "summary_json": payload,
    }
