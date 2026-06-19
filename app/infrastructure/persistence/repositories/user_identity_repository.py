"""Persistence adapter for external user identities and magic-link tokens."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.time_utils import utc_now
from app.db.models import MagicLinkToken, User, UserCredential, UserIdentity, model_to_dict

if TYPE_CHECKING:
    from app.db.session import Database


@dataclass(frozen=True)
class MagicLinkIssue:
    """Plain magic-link token returned once for email delivery."""

    user_id: int
    email: str
    token: str
    expires_at: Any


class UserIdentityRepository:
    """Repository for passwordless and social identities."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_identity(self, *, provider: str, subject: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            identity = await session.scalar(
                select(UserIdentity).where(
                    UserIdentity.provider == provider,
                    UserIdentity.subject == subject,
                )
            )
            return model_to_dict(identity)

    async def async_find_user_id_by_email(self, email_canonical: str) -> int | None:
        async with self._database.session() as session:
            identity_user_id = await session.scalar(
                select(UserIdentity.user_id).where(
                    UserIdentity.email_canonical == email_canonical,
                    UserIdentity.email_verified.is_(True),
                )
            )
            if identity_user_id is not None:
                return int(identity_user_id)

            credential_user_id = await session.scalar(
                select(UserCredential.user_id).where(
                    UserCredential.email_canonical == email_canonical
                )
            )
            if credential_user_id is not None:
                return int(credential_user_id)
            return None

    async def async_upsert_identity(
        self,
        *,
        user_id: int,
        provider: str,
        subject: str,
        email: str | None,
        email_canonical: str | None,
        email_verified: bool,
        display_name: str | None = None,
        touch_login: bool = True,
    ) -> dict[str, Any]:
        now = utc_now()
        async with self._database.transaction() as session:
            user = await session.get(User, user_id)
            if user is None:
                msg = f"User {user_id} not found"
                raise ValueError(msg)
            identity = await session.scalar(
                select(UserIdentity).where(
                    UserIdentity.provider == provider,
                    UserIdentity.subject == subject,
                )
            )
            if identity is None:
                identity = UserIdentity(
                    user_id=user_id,
                    provider=provider,
                    subject=subject,
                    email=email,
                    email_canonical=email_canonical,
                    email_verified=email_verified,
                    display_name=display_name,
                    last_login_at=now if touch_login else None,
                )
                session.add(identity)
            else:
                identity.user_id = user_id
                identity.email = email
                identity.email_canonical = email_canonical
                identity.email_verified = email_verified
                identity.display_name = display_name or identity.display_name
                if touch_login:
                    identity.last_login_at = now
                identity.updated_at = now
            await session.flush()
            return model_to_dict(identity) or {}

    async def async_issue_magic_link(
        self,
        *,
        user_id: int,
        email: str,
        email_canonical: str,
        client_id: str,
        ttl: timedelta = timedelta(minutes=15),
    ) -> MagicLinkIssue:
        token = secrets.token_urlsafe(32)
        expires_at = utc_now() + ttl
        async with self._database.transaction() as session:
            session.add(
                MagicLinkToken(
                    user_id=user_id,
                    email=email,
                    email_canonical=email_canonical,
                    token_hash=_hash_token(token),
                    client_id=client_id,
                    expires_at=expires_at,
                )
            )
        return MagicLinkIssue(
            user_id=user_id,
            email=email,
            token=token,
            expires_at=expires_at,
        )

    async def async_consume_magic_link(self, token: str) -> dict[str, Any] | None:
        token_hash = _hash_token(token)
        now = utc_now()
        async with self._database.transaction() as session:
            record = await session.scalar(
                select(MagicLinkToken).where(
                    MagicLinkToken.token_hash == token_hash,
                    MagicLinkToken.consumed_at.is_(None),
                    MagicLinkToken.expires_at > now,
                )
            )
            if record is None:
                return None
            record.consumed_at = now
            await session.flush()
            return model_to_dict(record)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
