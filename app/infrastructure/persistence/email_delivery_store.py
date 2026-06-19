"""Persistence helpers for email addresses and outbound email deliveries."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select

from app.core.time_utils import utc_now
from app.db.models import EmailDelivery, UserEmailAddress
from app.db.runtime_database import resolve_runtime_database


@dataclass(frozen=True)
class EmailVerificationToken:
    """Plain verification token returned once for delivery."""

    address: UserEmailAddress
    token: str


class EmailDeliveryStore:
    """Centralized ORM access for email delivery state."""

    def __init__(self, database: Any | None = None) -> None:
        self._db = database

    def _database(self) -> Any:
        return self._db or resolve_runtime_database()

    async def async_list_addresses(self, user_id: int) -> list[UserEmailAddress]:
        async with self._database().session() as session:
            return list(
                (
                    await session.execute(
                        select(UserEmailAddress)
                        .where(UserEmailAddress.user_id == user_id)
                        .order_by(UserEmailAddress.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )

    async def async_get_address_for_user(
        self,
        *,
        user_id: int,
        address_id: int,
    ) -> UserEmailAddress | None:
        async with self._database().session() as session:
            return cast(
                "UserEmailAddress | None",
                await session.scalar(
                    select(UserEmailAddress).where(
                        UserEmailAddress.id == address_id,
                        UserEmailAddress.user_id == user_id,
                    )
                ),
            )

    async def async_get_verified_address_for_user(
        self,
        *,
        user_id: int,
        address_id: int | None,
    ) -> UserEmailAddress | None:
        async with self._database().session() as session:
            stmt = select(UserEmailAddress).where(
                UserEmailAddress.user_id == user_id,
                UserEmailAddress.is_verified.is_(True),
            )
            if address_id is not None:
                stmt = stmt.where(UserEmailAddress.id == address_id)
            return cast(
                "UserEmailAddress | None",
                await session.scalar(stmt.order_by(UserEmailAddress.verified_at.desc())),
            )

    async def async_start_verification(
        self,
        *,
        user_id: int,
        email: str,
        email_canonical: str,
        ttl: timedelta = timedelta(hours=24),
    ) -> EmailVerificationToken:
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        expires_at = utc_now() + ttl
        async with self._database().transaction() as session:
            address = await session.scalar(
                select(UserEmailAddress).where(
                    UserEmailAddress.user_id == user_id,
                    UserEmailAddress.email_canonical == email_canonical,
                )
            )
            if address is None:
                address = UserEmailAddress(
                    user_id=user_id,
                    email=email,
                    email_canonical=email_canonical,
                )
                session.add(address)
            address.email = email
            address.confirmation_token_hash = token_hash
            address.confirmation_expires_at = expires_at
            address.updated_at = utc_now()
            await session.flush()
            return EmailVerificationToken(address=address, token=token)

    async def async_verify_token(self, token: str) -> UserEmailAddress | None:
        token_hash = _hash_token(token)
        now = utc_now()
        async with self._database().transaction() as session:
            address = await session.scalar(
                select(UserEmailAddress).where(
                    UserEmailAddress.confirmation_token_hash == token_hash,
                    UserEmailAddress.confirmation_expires_at > now,
                )
            )
            if address is None:
                return None
            address.is_verified = True
            address.verified_at = now
            address.confirmation_token_hash = None
            address.confirmation_expires_at = None
            address.updated_at = now
            await session.flush()
            return cast("UserEmailAddress", address)

    async def async_record_delivery(
        self,
        *,
        user_id: int,
        email_address_id: int | None,
        provider: str,
        recipient: str,
        subject: str,
        status: str,
        purpose: str,
        correlation_id: str | None,
        provider_message_id: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EmailDelivery:
        async with self._database().transaction() as session:
            delivery = EmailDelivery(
                user_id=user_id,
                email_address_id=email_address_id,
                provider=provider,
                recipient=recipient,
                subject=subject,
                status=status,
                purpose=purpose,
                correlation_id=correlation_id,
                provider_message_id=provider_message_id,
                error=error,
                metadata_json=metadata or {},
                delivered_at=utc_now() if status == "sent" else None,
            )
            session.add(delivery)
            await session.flush()
            return delivery


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
