from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure.persistence.repositories.audit_log_repository import (
    AuditLogRepositoryAdapter,
)


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        for index, item in enumerate(self.added, start=1):
            item.id = index


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.session = _Session()

    def transaction(self) -> _Transaction:
        return _Transaction(self.session)


@pytest.mark.asyncio
async def test_audit_log_repository_redacts_token_like_details_before_persisting() -> None:
    database = _Database()
    repo = AuditLogRepositoryAdapter(database)  # type: ignore[arg-type]

    log_id = await repo.async_insert_audit_log(
        "INFO",
        "github.integration.test",
        {
            "personal_access_token": "ghp_personalAccessSecretValue123456",
            "nested": {
                "client_secret": "oauth-client-secret-value",
                "device_code": "device-secret-code-value",
            },
            "ok": True,
        },
    )

    assert log_id == 1
    [log] = database.session.added
    assert log.details_json == {
        "personal_access_token": "[REDACTED]",
        "nested": {
            "client_secret": "[REDACTED]",
            "device_code": "[REDACTED]",
        },
        "ok": True,
    }
