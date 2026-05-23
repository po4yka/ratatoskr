"""SQLAlchemy adapter for encrypted social connection persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.application.ports.social_connections import (
    SUPPORTED_SOCIAL_PROVIDERS,
    SocialAuthStateCreate,
    SocialAuthStateRecord,
    SocialConnectionRecord,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
    SocialFetchAttemptCreate,
)
from app.db.models.social import (
    SocialAuthState,
    SocialAuthStateStatus,
    SocialAuthType,
    SocialConnection,
    SocialConnectionStatus,
    SocialFetchAttempt,
    SocialFetchAttemptStatus,
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

    async def list_by_user(self, user_id: int) -> list[SocialConnectionRecord]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(SocialConnection)
                    .where(SocialConnection.user_id == user_id)
                    .order_by(SocialConnection.provider)
                )
            ).scalars()
            return [_to_record(row) for row in rows]

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
                        "last_used_at": values["last_used_at"],
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

    async def delete_connection(self, user_id: int, provider: str) -> bool:
        provider_value = _provider(provider)
        async with self._db.transaction() as session:
            deleted_id = await session.scalar(
                delete(SocialConnection)
                .where(
                    SocialConnection.user_id == user_id,
                    SocialConnection.provider == provider_value,
                )
                .returning(SocialConnection.id)
            )
            return deleted_id is not None

    async def create_auth_state(self, state: SocialAuthStateCreate) -> SocialAuthStateRecord:
        row = SocialAuthState(
            user_id=state.user_id,
            provider=_provider(state.provider),
            state_hash=state.state_hash,
            encrypted_code_verifier=state.encrypted_code_verifier,
            redirect_uri=state.redirect_uri,
            scopes=list(state.scopes),
            metadata_json=dict(state.metadata_json) if state.metadata_json is not None else None,
            expires_at=state.expires_at,
        )
        async with self._db.transaction() as session:
            session.add(row)
            await session.flush()
            return _auth_state_to_record(row)

    async def get_auth_state(self, provider: str, state_hash: str) -> SocialAuthStateRecord | None:
        provider_value = _provider(provider)
        async with self._db.session() as session:
            row = await session.scalar(
                select(SocialAuthState).where(
                    SocialAuthState.provider == provider_value,
                    SocialAuthState.state_hash == state_hash,
                )
            )
            return _auth_state_to_record(row) if row is not None else None

    async def mark_auth_state_consumed(self, state_id: int) -> SocialAuthStateRecord | None:
        now = _utcnow()
        async with self._db.transaction() as session:
            row = await session.scalar(
                update(SocialAuthState)
                .where(
                    SocialAuthState.id == state_id,
                    SocialAuthState.status == SocialAuthStateStatus.PENDING,
                )
                .values(status=SocialAuthStateStatus.CONSUMED, consumed_at=now)
                .returning(SocialAuthState)
            )
            return _auth_state_to_record(row) if row is not None else None

    async def mark_auth_state_expired(self, state_id: int) -> SocialAuthStateRecord | None:
        async with self._db.transaction() as session:
            row = await session.scalar(
                update(SocialAuthState)
                .where(SocialAuthState.id == state_id)
                .values(status=SocialAuthStateStatus.EXPIRED)
                .returning(SocialAuthState)
            )
            return _auth_state_to_record(row) if row is not None else None

    async def record_fetch_attempt(self, attempt: SocialFetchAttemptCreate) -> None:
        row = SocialFetchAttempt(
            connection_id=attempt.connection_id,
            user_id=attempt.user_id,
            provider=_provider(attempt.provider),
            attempt_type=attempt.attempt_type,
            status=_fetch_attempt_status(attempt.status),
            error_code=attempt.error_code,
            error_message=attempt.error_message,
            metadata_json=dict(attempt.metadata_json)
            if attempt.metadata_json is not None
            else None,
        )
        if attempt.status in {"succeeded", "failed"}:
            row.finished_at = _utcnow()
        async with self._db.transaction() as session:
            session.add(row)


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


def _fetch_attempt_status(value: str) -> SocialFetchAttemptStatus:
    try:
        return SocialFetchAttemptStatus(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported social fetch attempt status: {value}") from exc


def _auth_state_status(row: SocialAuthState) -> str:
    return row.status.value if hasattr(row.status, "value") else str(row.status)


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
        "last_used_at": connection.last_used_at,
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
    if update.last_used_at is not None:
        values["last_used_at"] = update.last_used_at
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
        last_used_at=row.last_used_at,
        status=status,
        metadata_json=dict(row.metadata_json) if isinstance(row.metadata_json, dict) else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _auth_state_to_record(row: SocialAuthState) -> SocialAuthStateRecord:
    provider = row.provider.value if hasattr(row.provider, "value") else str(row.provider)
    return SocialAuthStateRecord(
        id=row.id,
        user_id=row.user_id,
        provider=provider,
        state_hash=row.state_hash,
        encrypted_code_verifier=row.encrypted_code_verifier,
        redirect_uri=row.redirect_uri,
        scopes=list(row.scopes) if isinstance(row.scopes, list) else None,
        status=_auth_state_status(row),
        metadata_json=dict(row.metadata_json) if isinstance(row.metadata_json, dict) else None,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        created_at=row.created_at,
    )
