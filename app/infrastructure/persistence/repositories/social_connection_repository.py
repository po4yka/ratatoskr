"""SQLAlchemy adapter for encrypted social connection persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.application.ports.social_connections import (
    SUPPORTED_SOCIAL_PROVIDERS,
    SocialConnectionRecord,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
)
from app.db.models.social import (
    SocialAuthType,
    SocialConnection,
    SocialConnectionStatus,
    SocialProvider,
)
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class SocialConnectionRepositoryAdapter:
    """Social connection persistence backed by SQLAlchemy."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> SocialConnectionRecord | None:
        provider_value = _provider(provider)
        async with self._db.session() as session:
            row = await session.scalar(
                select(SocialConnection).where(
                    SocialConnection.user_id == user_id,
                    SocialConnection.provider == provider_value,
                )
            )
        return _to_record(row) if row is not None else None

    async def upsert_connection(self, connection: SocialConnectionUpsert) -> SocialConnectionRecord:
        values = _upsert_values(connection)
        async with self._db.transaction() as session:
            stmt = (
                insert(SocialConnection)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_social_connections_user_provider",
                    set_={
                        "auth_type": values["auth_type"],
                        "provider_user_id": values["provider_user_id"],
                        "provider_username": values["provider_username"],
                        "encrypted_access_token": values["encrypted_access_token"],
                        "encrypted_refresh_token": values["encrypted_refresh_token"],
                        "token_scopes": values["token_scopes"],
                        "access_token_expires_at": values["access_token_expires_at"],
                        "refresh_token_expires_at": values["refresh_token_expires_at"],
                        "status": values["status"],
                        "metadata_json": values["metadata_json"],
                        "updated_at": _utcnow(),
                    },
                )
                .returning(SocialConnection)
            )
            row = (await session.execute(stmt)).scalar_one()
            return _to_record(row)

    async def update_connection(
        self, user_id: int, provider: str, update: SocialConnectionUpdate
    ) -> SocialConnectionRecord | None:
        provider_value = _provider(provider)
        values = _update_values(update)
        if not values:
            return await self.get_by_user_and_provider(user_id, provider)
        values["updated_at"] = _utcnow()

        async with self._db.transaction() as session:
            row = await session.scalar(
                select(SocialConnection).where(
                    SocialConnection.user_id == user_id,
                    SocialConnection.provider == provider_value,
                )
            )
            if row is None:
                return None
            for key, value in values.items():
                setattr(row, key, value)
            await session.flush()
            return _to_record(row)


def _provider(value: str) -> SocialProvider:
    if value not in SUPPORTED_SOCIAL_PROVIDERS:
        raise ValueError(f"Unsupported social provider: {value}")
    return SocialProvider(value)


def _auth_type(value: str) -> SocialAuthType:
    try:
        return SocialAuthType(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported social auth type: {value}") from exc


def _status(value: str) -> SocialConnectionStatus:
    try:
        return SocialConnectionStatus(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported social connection status: {value}") from exc


def _upsert_values(connection: SocialConnectionUpsert) -> dict[str, Any]:
    return {
        "user_id": connection.user_id,
        "provider": _provider(connection.provider),
        "auth_type": _auth_type(connection.auth_type),
        "provider_user_id": connection.provider_user_id,
        "provider_username": connection.provider_username,
        "encrypted_access_token": connection.encrypted_access_token,
        "encrypted_refresh_token": connection.encrypted_refresh_token,
        "token_scopes": list(connection.token_scopes)
        if connection.token_scopes is not None
        else None,
        "access_token_expires_at": connection.access_token_expires_at,
        "refresh_token_expires_at": connection.refresh_token_expires_at,
        "status": _status(connection.status),
        "metadata_json": dict(connection.metadata_json)
        if connection.metadata_json is not None
        else None,
    }


def _update_values(update: SocialConnectionUpdate) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if update.auth_type is not None:
        values["auth_type"] = _auth_type(update.auth_type)
    if update.provider_user_id is not None:
        values["provider_user_id"] = update.provider_user_id
    if update.provider_username is not None:
        values["provider_username"] = update.provider_username
    if update.encrypted_access_token is not None:
        values["encrypted_access_token"] = update.encrypted_access_token
    if update.encrypted_refresh_token is not None:
        values["encrypted_refresh_token"] = update.encrypted_refresh_token
    if update.token_scopes is not None:
        values["token_scopes"] = list(update.token_scopes)
    if update.access_token_expires_at is not None:
        values["access_token_expires_at"] = update.access_token_expires_at
    if update.refresh_token_expires_at is not None:
        values["refresh_token_expires_at"] = update.refresh_token_expires_at
    if update.status is not None:
        values["status"] = _status(update.status)
    if update.metadata_json is not None:
        values["metadata_json"] = dict(update.metadata_json)
    return values


def _to_record(row: SocialConnection) -> SocialConnectionRecord:
    provider = row.provider.value if hasattr(row.provider, "value") else str(row.provider)
    auth_type = row.auth_type.value if hasattr(row.auth_type, "value") else str(row.auth_type)
    status = row.status.value if hasattr(row.status, "value") else str(row.status)
    return SocialConnectionRecord(
        id=row.id,
        user_id=row.user_id,
        provider=provider,
        auth_type=auth_type,
        provider_user_id=row.provider_user_id,
        provider_username=row.provider_username,
        encrypted_access_token=row.encrypted_access_token,
        encrypted_refresh_token=row.encrypted_refresh_token,
        token_scopes=list(row.token_scopes) if isinstance(row.token_scopes, list) else None,
        access_token_expires_at=row.access_token_expires_at,
        refresh_token_expires_at=row.refresh_token_expires_at,
        status=status,
        metadata_json=dict(row.metadata_json) if isinstance(row.metadata_json, dict) else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
