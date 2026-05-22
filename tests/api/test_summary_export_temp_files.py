from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest

from app.adapters.external.formatting import export_temp_files
from app.adapters.external.formatting.export_formatter import ExportFormatter
from app.api.dependencies import database as database_deps
from app.api.routers.content import summaries as summaries_router


class _FakeSummaryUseCase:
    async def get_summary_by_id_for_user(self, *, user_id: int, summary_id: int) -> dict[str, Any]:
        return {"id": summary_id, "user_id": user_id}


@pytest.mark.asyncio
async def test_export_response_keeps_file_until_background_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    export_path = tmp_path / "ratatoskr-export-private.md"
    export_path.write_text("private summary", encoding="utf-8")

    monkeypatch.setattr(database_deps, "get_session_manager", lambda: object())

    def fake_export_summary(
        self: ExportFormatter,
        summary_id: str,
        export_format: str,
        correlation_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        return str(export_path), "summary.md"

    monkeypatch.setattr(ExportFormatter, "export_summary", fake_export_summary)

    response = await summaries_router.export_summary(
        summary_id=123,
        format="md",
        user={"user_id": 77},
        use_case=_FakeSummaryUseCase(),  # type: ignore[arg-type]
    )

    assert export_path.exists()
    assert response.background is not None

    sent_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            assert export_path.exists()
        sent_messages.append(message)

    await response({"type": "http", "method": "GET", "path": "/", "headers": []}, receive, send)

    assert not export_path.exists()
    assert any(message["type"] == "http.response.body" for message in sent_messages)


def test_export_cleanup_failure_logs_sanitized_path(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    sensitive_path = tmp_path / "https-example.test-private-token-secret"
    sensitive_path.mkdir()

    with caplog.at_level(logging.WARNING, logger=export_temp_files.__name__):
        export_temp_files.cleanup_export_file(sensitive_path)

    assert sensitive_path.exists()
    assert "export_temp_file_cleanup_failed" in caplog.messages
    assert str(sensitive_path) not in caplog.text
    assert "private-token-secret" not in caplog.text
    assert all(not hasattr(record, "path") for record in caplog.records)
    assert all(hasattr(record, "path_ref") for record in caplog.records)


def test_export_temp_storage_uses_random_names_under_private_export_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_url = "https://example.test/private/path?token=secret"
    monkeypatch.setattr(export_temp_files.tempfile, "gettempdir", lambda: str(tmp_path))

    with export_temp_files.named_export_temp_file(suffix=".md", mode="w", encoding="utf-8") as fd:
        fd.write("private")
        temp_path = Path(fd.name)

    try:
        assert temp_path.parent == tmp_path / export_temp_files.EXPORT_TEMP_DIRNAME
        assert temp_path.name.startswith(export_temp_files.EXPORT_TEMP_PREFIX)
        assert "example.test" not in str(temp_path)
        assert "secret" not in str(temp_path)
        assert "token" not in str(temp_path)

        formatter = ExportFormatter.__new__(ExportFormatter)
        filename = formatter._generate_filename(
            {"url": secret_url, "metadata": {"title": secret_url}, "summary_250": ""},
            "md",
        )
        assert "example.test" not in filename
        assert "secret" not in filename
        assert "token" not in filename
    finally:
        export_temp_files.cleanup_export_file(temp_path)


def test_export_formatter_writes_markdown_to_private_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_url = "https://example.test/private/path?token=secret"
    monkeypatch.setattr(export_temp_files.tempfile, "gettempdir", lambda: str(tmp_path))
    formatter = ExportFormatter.__new__(ExportFormatter)

    file_path, filename = formatter._export_markdown(
        {
            "url": secret_url,
            "metadata": {"title": "Private Export"},
            "summary_250": "A short private summary.",
        },
        correlation_id="cid-export-test",
    )

    assert file_path is not None
    assert filename is not None
    temp_path = Path(file_path)
    try:
        assert temp_path.exists()
        assert temp_path.parent == tmp_path / export_temp_files.EXPORT_TEMP_DIRNAME
        assert temp_path.name.startswith(export_temp_files.EXPORT_TEMP_PREFIX)
        assert "example.test" not in str(temp_path)
        assert "secret" not in str(temp_path)
        assert "token" not in str(temp_path)
        assert "example.test" not in filename
        assert "secret" not in filename
        assert "token" not in filename
    finally:
        export_temp_files.cleanup_export_file(temp_path)


def test_stale_export_cleanup_removes_only_old_export_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(export_temp_files.tempfile, "gettempdir", lambda: str(tmp_path))
    export_dir = export_temp_files.get_export_temp_dir()
    old_file = export_dir / f"{export_temp_files.EXPORT_TEMP_PREFIX}old.md"
    fresh_file = export_dir / f"{export_temp_files.EXPORT_TEMP_PREFIX}fresh.md"
    unrelated_file = export_dir / "other-temp.md"
    old_file.write_text("old", encoding="utf-8")
    fresh_file.write_text("fresh", encoding="utf-8")
    unrelated_file.write_text("other", encoding="utf-8")
    now = time.time()
    os.utime(old_file, (now - 7200, now - 7200))
    os.utime(fresh_file, (now, now))
    os.utime(unrelated_file, (now - 7200, now - 7200))

    result = export_temp_files.cleanup_stale_export_files(max_age_seconds=3600, now=now)

    assert result["deleted"] == 1
    assert not old_file.exists()
    assert fresh_file.exists()
    assert unrelated_file.exists()
