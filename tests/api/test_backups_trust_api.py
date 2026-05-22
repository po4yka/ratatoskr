from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.error_handlers import api_exception_handler
from app.api.exceptions import APIException
from app.api.routers import backups
from app.api.routers.auth.tokens import create_access_token
from app.db.models import Request, User, UserBackup


def _headers(user_id: int) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


def _backup_zip(*, request_count: int = 0) -> bytes:
    manifest = {
        "version": "1.0",
        "schema_version": "1.0",
        "user_id": 1,
        "created_at": "2026-05-22T00:00:00+00:00",
        "counts": {
            "requests": request_count,
            "summaries": 0,
            "tags": 0,
            "summary_tags": 0,
            "collections": 0,
            "collection_items": 0,
            "highlights": 0,
        },
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(
            "requests.json",
            json.dumps(
                [
                    {
                        "id": idx + 1,
                        "type": "url",
                        "status": "completed",
                        "dedupe_hash": f"dry-run-{idx}",
                    }
                    for idx in range(request_count)
                ]
            ),
        )
        for name in (
            "summaries",
            "tags",
            "summary_tags",
            "collections",
            "collection_items",
            "highlights",
        ):
            archive.writestr(f"{name}.json", "[]")
        archive.writestr("preferences.json", "{}")
    return buf.getvalue()


class _FakeBackupRepository:
    def __init__(self, backup: dict) -> None:
        self.backup = backup
        self.updated: dict[str, object] | None = None

    async def async_get_backup(self, backup_id: int) -> dict | None:
        if backup_id != self.backup["id"]:
            return None
        if self.updated:
            return {**self.backup, **self.updated}
        return self.backup

    async def async_update_backup(self, backup_id: int, **fields: object) -> None:
        assert backup_id == self.backup["id"]
        self.updated = fields


def _client_for_backup_user(repo: _FakeBackupRepository, *, user_id: int) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(APIException, api_exception_handler)
    app.include_router(backups.router, prefix="/v1/backups")
    app.dependency_overrides[backups.get_current_user] = lambda: {"user_id": user_id}
    app.dependency_overrides[backups.get_backup_repository] = lambda: repo
    return TestClient(app)


def test_verify_backup_api_enforces_backup_ownership_without_postgres(tmp_path: Path) -> None:
    backup_path = tmp_path / "ratatoskr-backup-owner.zip"
    backup_path.write_bytes(_backup_zip())
    repo = _FakeBackupRepository(
        {
            "id": 42,
            "user": 1001,
            "type": "manual",
            "status": "completed",
            "file_path": str(backup_path),
            "file_size_bytes": backup_path.stat().st_size,
            "items_count": 0,
            "created_at": "2026-05-22T00:00:00+00:00",
            "updated_at": "2026-05-22T00:00:00+00:00",
        }
    )

    forbidden = _client_for_backup_user(repo, user_id=2002).post("/v1/backups/42/verify")
    assert forbidden.status_code == 404

    ok = _client_for_backup_user(repo, user_id=1001).post("/v1/backups/42/verify")
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["verificationStatus"] == "verified"
    assert data["checksumSha256"]


@pytest.mark.asyncio
async def test_restore_dry_run_reports_counts_without_mutating_db(
    client: TestClient,
    db,
) -> None:
    async with db.transaction() as session:
        session.add(User(telegram_user_id=8201, username="backup-dry-run"))

    response = client.post(
        "/v1/backups/restore/dry-run",
        files={"file": ("backup.zip", _backup_zip(request_count=2), "application/zip")},
        headers=_headers(8201),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["valid"] is True
    assert data["compatible"] is True
    assert data["schemaVersion"] == "1.0"
    assert data["counts"]["requests"] == 2
    assert data["estimatedAffectedRows"]["requests"] == 2

    async with db.session() as session:
        request_count = await session.scalar(select(Request).where(Request.user_id == 8201))
    assert request_count is None


@pytest.mark.asyncio
async def test_verify_backup_is_owner_only_and_updates_metadata(
    client: TestClient,
    db,
    tmp_path: Path,
) -> None:
    owner_id = 8301
    other_id = 8302
    backup_path = tmp_path / "ratatoskr-backup-8301-test.zip"
    backup_path.write_bytes(_backup_zip())
    async with db.transaction() as session:
        session.add_all(
            [
                User(telegram_user_id=owner_id, username="backup-owner"),
                User(telegram_user_id=other_id, username="backup-other"),
            ]
        )
        backup = UserBackup(
            user_id=owner_id,
            type="manual",
            status="completed",
            file_path=str(backup_path),
            file_size_bytes=backup_path.stat().st_size,
        )
        session.add(backup)
        await session.flush()
        backup_id = backup.id

    forbidden = client.post(f"/v1/backups/{backup_id}/verify", headers=_headers(other_id))
    assert forbidden.status_code == 404

    ok = client.post(f"/v1/backups/{backup_id}/verify", headers=_headers(owner_id))
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["verificationStatus"] == "verified"
    assert data["verificationError"] is None
    assert data["checksumSha256"]
    assert data["schemaVersion"] == "1.0"
    assert data["itemCounts"]["requests"] == 0
