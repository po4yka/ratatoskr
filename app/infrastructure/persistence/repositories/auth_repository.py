"""SQLAlchemy implementation of auth repository."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, select, update

from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.db.models import ClientSecret, RefreshToken, User, model_to_dict

if TYPE_CHECKING:
    from app.db.session import Database
    from app.infrastructure.cache.auth_token_cache import AuthTokenCache

logger = get_logger(__name__)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


class AuthRepositoryAdapter:
    """Adapter for authentication-related operations."""

    def __init__(
        self,
        session_manager: Database,
        token_cache: AuthTokenCache | None = None,
    ) -> None:
        self._database = session_manager
        self._token_cache = token_cache

    async def async_create_refresh_token(
        self,
        *,
        user_id: int,
        token_hash: str,
        client_id: str | None,
        device_info: str | None,
        ip_address: str | None,
        expires_at: dt.datetime,
        remember_me: bool = True,
        family_id: str,
        parent_token_hash: str | None = None,
    ) -> int:
        """Create a new refresh token record.

        ``family_id`` is REQUIRED: callers must either generate a fresh UUID
        for the root of a new family (first login) or pass the predecessor's
        family_id when rotating. ``parent_token_hash`` is the sha256 of the
        token being rotated out (NULL for the family root). Both columns
        feed the :class:`TokenFamilyPolicy` decision in /refresh.
        """
        async with self._database.transaction() as session:
            record = RefreshToken(
                user_id=user_id,
                token_hash=token_hash,
                client_id=client_id,
                device_info=device_info,
                ip_address=ip_address,
                expires_at=expires_at,
                is_revoked=False,
                remember_me=remember_me,
                family_id=family_id,
                parent_token_hash=parent_token_hash,
            )
            session.add(record)
            await session.flush()
            token_id = record.id

        if self._token_cache:
            try:
                await self._token_cache.set_token(
                    token_hash,
                    user_id=user_id,
                    client_id=client_id,
                    expires_at=expires_at,
                    is_revoked=False,
                    token_id=token_id,
                    remember_me=remember_me,
                    family_id=family_id,
                    parent_token_hash=parent_token_hash,
                )
            except Exception as exc:
                logger.warning(
                    "auth_token_cache_write_failed",
                    extra={"error": str(exc), "token_hash_prefix": token_hash[:8]},
                )

        return token_id

    async def async_get_family_records(
        self, family_id: str, owner_user_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return every refresh-token row sharing ``family_id``.

        Always reads from the DB (no cache) so the policy sees a consistent
        snapshot — required to decide REVOKE_FAMILY vs ROTATE correctly under
        concurrent refresh.

        When ``owner_user_id`` is provided the query includes a
        ``RefreshToken.user_id == owner_user_id`` predicate so that a
        manipulated ``family_id`` cannot expose another user's token rows.
        Callers that know the authenticated user should always pass it.
        """
        async with self._database.session() as session:
            stmt = select(RefreshToken).where(RefreshToken.family_id == family_id)
            if owner_user_id is not None:
                stmt = stmt.where(RefreshToken.user_id == owner_user_id)
            rows = (await session.execute(stmt.order_by(RefreshToken.id))).scalars()
            return [_token_to_dict(row) or {} for row in rows]

    async def async_revoke_family(
        self, family_id: str, owner_user_id: int | None = None
    ) -> list[str]:
        """Mark every token in ``family_id`` revoked.

        Returns the list of token hashes that flipped to revoked so the
        caller can invalidate the token cache. Already-revoked rows are
        not re-touched.

        When ``owner_user_id`` is provided the UPDATE includes a
        ``RefreshToken.user_id == owner_user_id`` predicate so that a
        manipulated ``family_id`` cannot revoke another user's tokens.
        Callers that know the authenticated user should always pass it.
        """
        async with self._database.transaction() as session:
            conditions = [
                RefreshToken.family_id == family_id,
                RefreshToken.is_revoked.is_(False),
            ]
            if owner_user_id is not None:
                conditions.append(RefreshToken.user_id == owner_user_id)
            hashes = list(
                (
                    await session.execute(
                        update(RefreshToken)
                        .where(*conditions)
                        .values(is_revoked=True)
                        .returning(RefreshToken.token_hash)
                    )
                ).scalars()
            )

        if hashes and self._token_cache:
            for token_hash in hashes:
                try:
                    await self._token_cache.mark_revoked(token_hash)
                except Exception as exc:
                    logger.warning(
                        "auth_token_cache_revoke_failed",
                        extra={"error": str(exc), "token_hash_prefix": token_hash[:8]},
                    )

        return hashes

    async def async_list_active_family_records_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """Return every active (non-revoked, non-expired) refresh-token row
        for a user. Used by ``POST /v1/auth/logout-all`` to enumerate the
        distinct families to revoke.
        """
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(RefreshToken)
                    .where(
                        RefreshToken.user_id == user_id,
                        RefreshToken.is_revoked.is_(False),
                        RefreshToken.expires_at > _utcnow(),
                    )
                    .order_by(RefreshToken.id)
                )
            ).scalars()
            return [_token_to_dict(row) or {} for row in rows]

    async def async_get_refresh_token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        """Get a refresh token by hash, using cache when available."""
        if self._token_cache:
            try:
                cached = await self._token_cache.get_token(token_hash)
                if cached is not None:
                    return cached
            except Exception as exc:
                logger.warning(
                    "auth_token_cache_read_failed",
                    extra={"error": str(exc), "token_hash_prefix": token_hash[:8]},
                )

        async with self._database.session() as session:
            record = await session.scalar(
                select(RefreshToken).where(RefreshToken.token_hash == token_hash)
            )
            result = _token_to_dict(record)

        if result and self._token_cache:
            try:
                await self._token_cache.set_token(
                    token_hash,
                    user_id=result.get("user"),
                    client_id=result.get("client_id"),
                    expires_at=result.get("expires_at"),
                    is_revoked=result.get("is_revoked", False),
                    token_id=result.get("id"),
                    remember_me=result.get("remember_me", True),
                    family_id=result.get("family_id"),
                    parent_token_hash=result.get("parent_token_hash"),
                )
            except Exception as exc:
                logger.warning(
                    "auth_token_cache_populate_failed",
                    extra={"error": str(exc), "token_hash_prefix": token_hash[:8]},
                )

        return result

    async def async_revoke_refresh_token(self, token_hash: str) -> bool:
        """Atomically revoke an active refresh token by hash.

        Returning ``False`` for an already-revoked token lets refresh rotation
        distinguish the winner of a concurrent compare-and-set from a replay.
        """
        async with self._database.transaction() as session:
            revoked_id = await session.scalar(
                update(RefreshToken)
                .where(
                    RefreshToken.token_hash == token_hash,
                    RefreshToken.is_revoked.is_(False),
                )
                .values(is_revoked=True)
                .returning(RefreshToken.id)
            )
            revoked = revoked_id is not None

        if revoked and self._token_cache:
            try:
                await self._token_cache.mark_revoked(token_hash)
            except Exception as exc:
                logger.warning(
                    "auth_token_cache_revoke_failed",
                    extra={"error": str(exc), "token_hash_prefix": token_hash[:8]},
                )

        return revoked

    async def async_revoke_session_by_id(self, session_id: int, user_id: int) -> bool:
        """Revoke a specific session by ID, ensuring it belongs to a user."""
        async with self._database.transaction() as session:
            token_hash = await session.scalar(
                update(RefreshToken)
                .where(
                    RefreshToken.id == session_id,
                    RefreshToken.user_id == user_id,
                    RefreshToken.is_revoked.is_(False),
                )
                .values(is_revoked=True)
                .returning(RefreshToken.token_hash)
            )
            revoked = token_hash is not None

        if revoked and self._token_cache and token_hash:
            try:
                await self._token_cache.mark_revoked(token_hash)
            except Exception as exc:
                logger.warning(
                    "auth_token_cache_revoke_failed",
                    extra={"error": str(exc), "session_id": session_id},
                )

        return revoked

    async def async_revoke_all_user_tokens(self, user_id: int) -> int:
        """Revoke all active refresh tokens for a user."""
        async with self._database.transaction() as session:
            hashes = list(
                (
                    await session.execute(
                        update(RefreshToken)
                        .where(
                            RefreshToken.user_id == user_id,
                            RefreshToken.is_revoked.is_(False),
                        )
                        .values(is_revoked=True)
                        .returning(RefreshToken.token_hash)
                    )
                ).scalars()
            )

        if hashes and self._token_cache:
            for token_hash in hashes:
                try:
                    await self._token_cache.mark_revoked(token_hash)
                except Exception as exc:
                    logger.warning(
                        "auth_token_cache_revoke_failed",
                        extra={"error": str(exc), "token_hash_prefix": token_hash[:8]},
                    )

        return len(hashes)

    async def async_update_refresh_token_last_used(self, token_id: int) -> None:
        """Update the last-used timestamp for a refresh token."""
        async with self._database.transaction() as session:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.id == token_id)
                .values(last_used_at=_utcnow())
            )

    async def async_list_active_sessions(
        self, user_id: int, now: dt.datetime
    ) -> list[dict[str, Any]]:
        """List active non-revoked, non-expired sessions."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(RefreshToken)
                    .where(
                        RefreshToken.user_id == user_id,
                        RefreshToken.is_revoked.is_(False),
                        RefreshToken.expires_at > now,
                    )
                    .order_by(RefreshToken.last_used_at.desc())
                )
            ).scalars()
            return [_token_to_dict(row) or {} for row in rows]

    async def async_get_client_secret(self, user_id: int, client_id: str) -> dict[str, Any] | None:
        """Get the most recent client secret for a user/client pair."""
        async with self._database.session() as session:
            user_exists = await session.scalar(
                select(User.telegram_user_id).where(User.telegram_user_id == user_id)
            )
            if user_exists is None:
                return None
            record = await session.scalar(
                select(ClientSecret)
                .where(ClientSecret.user_id == user_id, ClientSecret.client_id == client_id)
                .order_by(ClientSecret.created_at.desc())
            )
            return _secret_to_dict(record)

    async def async_get_client_secret_by_id(self, key_id: int) -> dict[str, Any] | None:
        """Get a client secret by ID."""
        async with self._database.session() as session:
            record = await session.get(ClientSecret, key_id)
            return _secret_to_dict(record)

    async def async_create_client_secret(
        self,
        *,
        user_id: int,
        client_id: str,
        secret_hash: str,
        secret_salt: str,
        status: str = "active",
        label: str | None = None,
        description: str | None = None,
        expires_at: dt.datetime | None = None,
    ) -> int:
        """Create a new client secret."""
        async with self._database.transaction() as session:
            if await session.get(User, user_id) is None:
                msg = f"User {user_id} not found"
                raise ValueError(msg)
            record = ClientSecret(
                user_id=user_id,
                client_id=client_id,
                secret_hash=secret_hash,
                secret_salt=secret_salt,
                status=status,
                label=label,
                description=description,
                expires_at=expires_at,
                failed_attempts=0,
                locked_until=None,
            )
            session.add(record)
            await session.flush()
            return record.id

    async def async_replace_active_client_secret(
        self,
        *,
        user_id: int,
        client_id: str,
        secret_hash: str,
        secret_salt: str,
        status: str = "active",
        label: str | None = None,
        description: str | None = None,
        expires_at: dt.datetime | None = None,
    ) -> int:
        """Atomically revoke active secrets for a client and create a replacement."""
        async with self._database.transaction() as session:
            if await session.get(User, user_id) is None:
                msg = f"User {user_id} not found"
                raise ValueError(msg)

            await session.execute(
                update(ClientSecret)
                .where(
                    ClientSecret.user_id == user_id,
                    ClientSecret.client_id == client_id,
                    ClientSecret.status == "active",
                )
                .values(status="revoked", failed_attempts=0, locked_until=None)
            )
            record = ClientSecret(
                user_id=user_id,
                client_id=client_id,
                secret_hash=secret_hash,
                secret_salt=secret_salt,
                status=status,
                label=label,
                description=description,
                expires_at=expires_at,
                failed_attempts=0,
                locked_until=None,
            )
            session.add(record)
            await session.flush()
            return record.id

    async def async_update_client_secret(
        self,
        key_id: int,
        owner_user_id: int | None = None,
        **fields: Any,
    ) -> None:
        """Update a client secret by ID.

        When ``owner_user_id`` is provided the UPDATE includes a
        ``ClientSecret.user_id == owner_user_id`` predicate so that a stale
        or manipulated ``key_id`` cannot mutate a row owned by a different
        user. Callers that know the owner should always pass it.
        """
        allowed = set(ClientSecret.__table__.columns.keys()) - {"id", "user_id"}
        update_fields = {key: value for key, value in fields.items() if key in allowed}
        if not update_fields:
            return
        async with self._database.transaction() as session:
            stmt = update(ClientSecret).where(ClientSecret.id == key_id)
            if owner_user_id is not None:
                stmt = stmt.where(ClientSecret.user_id == owner_user_id)
            await session.execute(stmt.values(**update_fields))

    async def async_list_client_secrets(
        self,
        *,
        user_id: int | None = None,
        client_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List client secrets with optional filters."""
        async with self._database.session() as session:
            stmt = select(ClientSecret)
            if user_id is not None:
                stmt = stmt.where(ClientSecret.user_id == user_id)
            if client_id:
                stmt = stmt.where(ClientSecret.client_id == client_id)
            if status:
                stmt = stmt.where(ClientSecret.status == status)
            rows = (await session.execute(stmt.order_by(ClientSecret.id))).scalars()
            return [_secret_to_dict(row) or {} for row in rows]

    async def async_increment_failed_attempts(
        self, key_id: int, max_attempts: int, lockout_minutes: int
    ) -> dict[str, Any]:
        """Atomically increment failed_attempts; lock when threshold is reached.

        Single SQL UPDATE ... RETURNING (Postgres MVCC handles concurrent
        increments safely under row-level locking) -- avoids the
        read-modify-write race that a pure-Python increment would hit.
        """
        async with self._database.transaction() as session:
            interval = func.make_interval(0, 0, 0, 0, 0, lockout_minutes, 0)
            stmt = (
                update(ClientSecret)
                .where(ClientSecret.id == key_id)
                .values(
                    failed_attempts=ClientSecret.failed_attempts + 1,
                    status=case(
                        (
                            ClientSecret.failed_attempts + 1 >= max_attempts,
                            "locked",
                        ),
                        else_=ClientSecret.status,
                    ),
                    locked_until=case(
                        (
                            ClientSecret.failed_attempts + 1 >= max_attempts,
                            func.now() + interval,
                        ),
                        else_=ClientSecret.locked_until,
                    ),
                )
                .returning(ClientSecret)
            )
            result = await session.scalars(stmt)
            record = result.one_or_none()
            return _secret_to_dict(record) or {}

    async def async_reset_failed_attempts(self, key_id: int) -> None:
        """Reset failed attempts and unlock a secret."""
        async with self._database.transaction() as session:
            await session.execute(
                update(ClientSecret)
                .where(ClientSecret.id == key_id)
                .values(failed_attempts=0, locked_until=None)
            )


def _token_to_dict(record: RefreshToken | None) -> dict[str, Any] | None:
    data = model_to_dict(record)
    if data is not None:
        data["user"] = data.get("user_id")
    return data


def _secret_to_dict(record: ClientSecret | None) -> dict[str, Any] | None:
    data = model_to_dict(record)
    if data is not None:
        data["user"] = data.get("user_id")
    return data
