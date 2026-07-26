"""Upload size and item-count limit tests for import and backup-restore endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.routers.auth.tokens import create_access_token
from app.config import Config


def _auth(telegram_id: int) -> dict[str, str]:
    token = create_access_token(telegram_id, client_id="test_client")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# import — oversized file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_file_too_large(client: TestClient, db, user_factory):
    user_id = int(Config.get_allowed_user_ids()[0])
    user = await user_factory(telegram_user_id=user_id, username="upload_limit_import_size")
    headers = _auth(user.telegram_user_id)

    mock_cfg = MagicMock()
    mock_cfg.import_export.max_upload_bytes = 10
    mock_cfg.import_export.max_items = 10_000

    with patch("app.api.routers.import_export.load_config", return_value=mock_cfg):
        response = client.post(
            "/v1/import",
            files={"file": ("bookmarks.html", b"x" * 11, "text/html")},
            data={"options": "{}"},
            headers=headers,
        )

    assert response.status_code == 413


# ---------------------------------------------------------------------------
# backup restore — oversized file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_file_too_large(client: TestClient, db, user_factory):
    user_id = int(Config.get_allowed_user_ids()[0])
    user = await user_factory(telegram_user_id=user_id, username="upload_limit_restore_size")
    headers = _auth(user.telegram_user_id)

    mock_backup_cfg = MagicMock()
    mock_backup_cfg.max_restore_bytes = 10

    with patch("app.api.routers.backups.load_backup_config", return_value=mock_backup_cfg):
        response = client.post(
            "/v1/backups/restore",
            files={"file": ("backup.zip", b"x" * 11, "application/zip")},
            headers=headers,
        )

    assert response.status_code == 413


# ---------------------------------------------------------------------------
# import — too many parsed bookmarks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_too_many_bookmarks(client: TestClient, db, user_factory):
    user_id = int(Config.get_allowed_user_ids()[0])
    user = await user_factory(telegram_user_id=user_id, username="upload_limit_import_items")
    headers = _auth(user.telegram_user_id)

    mock_cfg = MagicMock()
    mock_cfg.import_export.max_upload_bytes = 10_000
    mock_cfg.import_export.max_items = 2  # limit 2; parser returns 3

    fake_bookmarks = [{"url": f"https://example.com/{i}"} for i in range(3)]
    mock_parser_cls = MagicMock()
    mock_parser_cls.return_value.parse.return_value = fake_bookmarks

    with (
        patch("app.api.routers.import_export.load_config", return_value=mock_cfg),
        patch("app.api.routers.import_export.FormatDetector.detect", return_value="html"),
        patch("app.api.routers.import_export.PARSER_REGISTRY", {"html": mock_parser_cls}),
    ):
        response = client.post(
            "/v1/import",
            files={"file": ("bookmarks.html", b"data", "text/html")},
            data={"options": "{}"},
            headers=headers,
        )

    assert response.status_code == 400
    body = response.json()
    assert "3" in body["error"]["message"]
    assert "2" in body["error"]["message"]


# ---------------------------------------------------------------------------
# import — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_success(client: TestClient, db, user_factory):
    from unittest.mock import patch as _patch

    from app.tasks.import_tasks import process_import_job

    user_id = int(Config.get_allowed_user_ids()[0])
    user = await user_factory(telegram_user_id=user_id, username="upload_limit_import_ok")
    headers = _auth(user.telegram_user_id)

    mock_cfg = MagicMock()
    mock_cfg.import_export.max_upload_bytes = 10_000
    mock_cfg.import_export.max_items = 100

    fake_bookmarks = [MagicMock(url="https://example.com/1", created_at=None)]
    mock_parser_cls = MagicMock()
    mock_parser_cls.return_value.parse.return_value = fake_bookmarks

    mock_job = {"id": 99, "status": "pending", "total_items": 1}

    with (
        _patch("app.api.routers.import_export.load_config", return_value=mock_cfg),
        _patch("app.api.routers.import_export.FormatDetector.detect", return_value="html"),
        _patch("app.api.routers.import_export.PARSER_REGISTRY", {"html": mock_parser_cls}),
        _patch(
            "app.api.routers.import_export.ImportExportService.create_import_job",
            new_callable=AsyncMock,
            return_value=mock_job,
        ),
        _patch.object(process_import_job, "kiq", new_callable=AsyncMock) as mock_kiq,
    ):
        response = client.post(
            "/v1/import",
            files={"file": ("bookmarks.html", b"some data", "text/html")},
            data={"options": "{}"},
            headers=headers,
        )

    assert response.status_code == 201
    assert response.json()["data"]["id"] == 99
    mock_kiq.assert_awaited_once()
