from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.routers import backups, webhooks
from app.api.services.import_export_service import ImportExportService


class _BackupRepository:
    def __init__(self, backup: dict | None) -> None:
        self.async_get_backup = AsyncMock(return_value=backup)


class _WebhookRepository:
    def __init__(self, subscription: dict | None) -> None:
        self.async_get_subscription_by_id = AsyncMock(return_value=subscription)


class _ImportJobRepository:
    def __init__(self, job: dict | None) -> None:
        self.async_get_job = AsyncMock(return_value=job)


@pytest.mark.asyncio
async def test_backup_ownership_helper_rejects_non_owner() -> None:
    repo = _BackupRepository({"id": 5, "user": 2002})

    with pytest.raises(ResourceNotFoundError):
        await backups._verify_ownership(repo, backup_id=5, user_id=1001)

    repo.async_get_backup.assert_awaited_once_with(5)


@pytest.mark.asyncio
async def test_webhook_ownership_helper_rejects_non_owner() -> None:
    repo = _WebhookRepository({"id": 7, "user": 2002, "is_deleted": False})

    with pytest.raises(ResourceNotFoundError):
        await webhooks._verify_ownership(repo, webhook_id=7, user_id=1001)

    repo.async_get_subscription_by_id.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_import_job_ownership_helper_rejects_non_owner() -> None:
    service = object.__new__(ImportExportService)
    service._import_job_repo = _ImportJobRepository({"id": 9, "user": 2002})

    with pytest.raises(ResourceNotFoundError):
        await service._verify_job_ownership(job_id=9, user_id=1001)

    service._import_job_repo.async_get_job.assert_awaited_once_with(9)
