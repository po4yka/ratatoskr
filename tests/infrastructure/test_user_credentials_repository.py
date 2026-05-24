from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.db.models import User, UserCredential
from app.infrastructure.persistence.repositories.user_credentials_repository import (
    UserCredentialRepositoryAdapter,
)


class _ReturnedScalars:
    def __init__(self, value: Any) -> None:
        self._value = value

    def one_or_none(self) -> Any:
        return self._value


class _CredentialsSession:
    def __init__(
        self,
        *,
        scalar_values: list[Any] | None = None,
        get_values: list[Any] | None = None,
        scalars_values: list[Any] | None = None,
    ) -> None:
        self.scalar_values = list(scalar_values or [])
        self.get_values = list(get_values or [])
        self.scalars_values = list(scalars_values or [])
        self.added: list[Any] = []
        self.executed: list[Any] = []
        self.flush_count = 0

    async def scalar(self, statement: Any) -> Any:
        self.executed.append(statement)
        return self.scalar_values.pop(0) if self.scalar_values else None

    async def scalars(self, statement: Any) -> _ReturnedScalars:
        self.executed.append(statement)
        value = self.scalars_values.pop(0) if self.scalars_values else None
        return _ReturnedScalars(value)

    async def get(self, model: Any, key: Any) -> Any:
        return self.get_values.pop(0) if self.get_values else None

    def add(self, instance: Any) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = 100 + len(self.added)
        self.added.append(instance)

    async def flush(self) -> None:
        self.flush_count += 1

    async def execute(self, statement: Any) -> None:
        self.executed.append(statement)


class _CredentialsDb:
    def __init__(self, session: _CredentialsSession) -> None:
        self.session_obj = session

    @asynccontextmanager
    async def session(self):
        yield self.session_obj

    @asynccontextmanager
    async def transaction(self):
        yield self.session_obj


def _credential(**overrides: Any) -> UserCredential:
    values = {
        "id": 1,
        "user_id": 10,
        "nickname": "Owner",
        "nickname_canonical": "owner",
        "email": "owner@example.com",
        "email_canonical": "owner@example.com",
        "password_hash": "hash-old",
        "pepper_version": 1,
        "failed_attempts": 0,
    }
    values.update(overrides)
    return UserCredential(**values)


@pytest.mark.asyncio
async def test_credentials_repository_lookup_create_update_and_lockout_state() -> None:
    credential = _credential()
    updated = _credential(id=2, failed_attempts=3)
    session = _CredentialsSession(
        scalar_values=[credential, credential, None, credential],
        get_values=[User(telegram_user_id=10), User(telegram_user_id=10)],
        scalars_values=[updated],
    )
    repo = UserCredentialRepositoryAdapter(_CredentialsDb(session))  # type: ignore[arg-type]

    assert await repo.async_get_by_canonical() is None
    by_nickname = await repo.async_get_by_canonical(nickname_canonical="owner")
    assert by_nickname["id"] == 1
    assert by_nickname["nickname_canonical"] == "owner"

    by_user = await repo.async_get_by_user_id(10)
    assert by_user["email_canonical"] == "owner@example.com"

    created_id = await repo.async_upsert(
        user_id=10,
        nickname="Owner",
        nickname_canonical="owner",
        email="owner@example.com",
        email_canonical="owner@example.com",
        password_hash="hash-new",
        pepper_version=2,
    )
    assert created_id == 100
    assert session.added[0].password_hash == "hash-new"

    updated_id = await repo.async_upsert(
        user_id=10,
        nickname="Changed",
        nickname_canonical="changed",
        email=None,
        email_canonical=None,
        password_hash="hash-updated",
        pepper_version=3,
    )
    assert updated_id == 1
    assert credential.nickname == "Changed"
    assert credential.failed_attempts == 0
    assert credential.password_hash == "hash-updated"

    lockout = await repo.async_record_failure(2, max_attempts=3, lockout_minutes=15)
    assert lockout["id"] == 2
    assert lockout["failed_attempts"] == 3

    await repo.async_reset_failure(2)
    await repo.async_touch_last_login(2, updated.password_updated_at)
    await repo.async_update_password_hash(2, password_hash="hash-final", pepper_version=4)
    assert session.flush_count == 2
    assert len(session.executed) >= 7


@pytest.mark.asyncio
async def test_credentials_repository_rejects_upsert_for_missing_user() -> None:
    session = _CredentialsSession(get_values=[None])
    repo = UserCredentialRepositoryAdapter(_CredentialsDb(session))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="User 404 not found"):
        await repo.async_upsert(
            user_id=404,
            nickname="Missing",
            nickname_canonical="missing",
            email=None,
            email_canonical=None,
            password_hash="hash",
            pepper_version=1,
        )
