from __future__ import annotations

from datetime import UTC, datetime
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
        self.logged_delivery: dict | None = None

    async def async_log_delivery(self, **kwargs: object) -> dict:
        self.logged_delivery = dict(kwargs)
        return {
            "id": 42,
            "event_type": kwargs["event_type"],
            "response_status": kwargs["response_status"],
            "success": kwargs["success"],
            "attempt": kwargs["attempt"],
            "duration_ms": kwargs["duration_ms"],
            "error": kwargs["error"],
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }


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
async def test_send_test_webhook_uses_safe_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    repo = _WebhookRepository(
        {
            "id": 7,
            "user": 1001,
            "is_deleted": False,
            "url": "https://example.com/hook",
            "secret": "secret",
        }
    )

    class _Response:
        status_code = 202
        text = "accepted"

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            calls["closed"] = True

        async def post(
            self,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
        ) -> _Response:
            calls["url"] = url
            calls["content"] = content
            calls["headers"] = headers
            return _Response()

    def _safe_client_factory(**kwargs: object) -> _Client:
        calls["client_kwargs"] = kwargs
        return _Client()

    monkeypatch.setattr("app.api.routers.webhooks.is_webhook_url_safe", lambda _url: (True, None))
    monkeypatch.setattr("app.api.routers.webhooks.make_safe_async_client", _safe_client_factory)

    result = await webhooks.send_test_webhook(
        7,
        user={"user_id": 1001},
        webhook_repo=repo,
    )

    assert result["data"]["success"] is True
    assert calls["client_kwargs"] == {"timeout": 10.0, "follow_redirects": False}
    assert calls["url"] == "https://example.com/hook"
    assert calls["closed"] is True
    assert isinstance(calls["content"], bytes)
    headers = calls["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Webhook-Event"] == "test"
    assert headers["X-Webhook-Signature"]
    assert repo.logged_delivery is not None
    assert repo.logged_delivery["response_status"] == 202
    assert repo.logged_delivery["response_body"] == "accepted"
    assert repo.logged_delivery["success"] is True


@pytest.mark.asyncio
async def test_send_test_webhook_logs_dns_rebinding_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _WebhookRepository(
        {
            "id": 7,
            "user": 1001,
            "is_deleted": False,
            "url": "https://rebind.example/hook",
            "secret": "secret",
        }
    )

    monkeypatch.setattr("app.api.routers.webhooks.is_webhook_url_safe", lambda _url: (True, None))
    monkeypatch.setattr(
        "app.security.ssrf.socket.getaddrinfo",
        lambda host, port, **_kwargs: [
            (0, 0, 0, "", ("10.0.0.1", port)),
        ],
    )

    result = await webhooks.send_test_webhook(
        7,
        user={"user_id": 1001},
        webhook_repo=repo,
    )

    assert result["data"]["success"] is False
    assert repo.logged_delivery is not None
    assert repo.logged_delivery["response_status"] is None
    assert repo.logged_delivery["response_body"] is None
    assert repo.logged_delivery["success"] is False
    assert "SSRF blocked" in str(repo.logged_delivery["error"])


@pytest.mark.asyncio
async def test_import_job_ownership_helper_rejects_non_owner() -> None:
    service = object.__new__(ImportExportService)
    service._import_job_repo = _ImportJobRepository({"id": 9, "user": 2002})

    with pytest.raises(ResourceNotFoundError):
        await service._verify_job_ownership(job_id=9, user_id=1001)

    service._import_job_repo.async_get_job.assert_awaited_once_with(9)
