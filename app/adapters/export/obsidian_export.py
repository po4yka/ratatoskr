"""Local Obsidian vault export adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.adapters.export.base import (
    ExportPayload,
    ExportResult,
    ensure_child_path,
    render_markdown,
    safe_markdown_filename,
)


class ObsidianExportAdapter:
    def __init__(self, *, vault_path: str, folder: str | None = None) -> None:
        if not vault_path:
            msg = "Obsidian export requires config.vault_path"
            raise ValueError(msg)
        self._vault_path = Path(vault_path)
        self._folder = folder.strip("/ ") if folder else ""

    async def export(self, payload: ExportPayload) -> ExportResult:
        filename = safe_markdown_filename(payload.title, payload.summary_id)
        relative_name = f"{self._folder}/{filename}" if self._folder else filename
        body = render_markdown(payload)
        path = ensure_child_path(self._vault_path, relative_name)
        await asyncio.to_thread(_write_file, path, body)
        return ExportResult(success=True, response_status=None, response_body=str(path))


def _write_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
