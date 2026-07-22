"""Read/write access to UI-managed service credentials.

Resolution order for a credential is **database first, environment second**, so
a value installed through the settings UI overrides the deployed ``.env`` while
an untouched key keeps working exactly as before. That ordering is what makes
the feature additive: nothing breaks for a deployment that never opens the UI.

Hot reload
----------
Resolved values are cached in-process for ``ttl_seconds``. A write through this
store clears the local cache immediately, so the writing process sees the new
key on its next call. The bot, worker, and API run as *separate containers*, so
they cannot share that invalidation -- the TTL is what bounds their staleness.

ponytail: TTL expiry, no cross-process pub/sub. A key change reaches every
process within ``ttl_seconds`` (default 30s) with no restart, which is the
actual requirement. If that ever needs to be instant, publish invalidations on
the existing Redis connection and keep this interface unchanged.

Plaintext never leaves this module except through :meth:`resolve`, and is never
logged. The API returns presence and a display ``hint`` only.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select

from app.config.credential_catalog import CATALOG, is_ui_managed
from app.core.logging_utils import get_logger
from app.db.models.credentials import ServiceCredential
from app.security.secret_crypto import InvalidEncryptedSecretError, decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

    from app.db.session import Database

logger = get_logger(__name__)

__all__ = ["CredentialStatus", "CredentialStore", "UnknownCredentialError"]

_HINT_CHARS = 4


class UnknownCredentialError(ValueError):
    """Raised when a key is not in the UI-manageable catalog."""


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Non-secret view of one catalog entry, safe to return over the API."""

    key: str
    label: str
    group: str
    help_url: str | None
    configured_in_db: bool
    configured_in_env: bool
    hint: str | None


def _hint_for(value: str) -> str | None:
    """Return a short display tail, or ``None`` when the value is too short."""
    stripped = value.strip()
    if len(stripped) <= _HINT_CHARS:
        return None
    return f"...{stripped[-_HINT_CHARS:]}"


class CredentialStore:
    """Encrypted credential persistence with a short-lived resolution cache."""

    def __init__(self, db: Database, *, ttl_seconds: float = 30.0) -> None:
        self._db = db
        self._ttl = ttl_seconds
        self._cache: dict[tuple[int, str], tuple[str | None, float]] = {}

    def invalidate(self) -> None:
        """Drop every cached resolution in this process."""
        self._cache.clear()

    async def resolve(self, key: str, *, user_id: int) -> str | None:
        """Return the plaintext credential, or ``None`` when unset.

        Checks the encrypted store first and falls back to the process
        environment. Never logs the value.
        """
        if not is_ui_managed(key):
            # Not catalog-managed: the environment is the only source.
            return os.environ.get(key) or None

        cache_key = (user_id, key)
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]

        value = await self._load_from_db(key, user_id=user_id)
        if value is None:
            value = os.environ.get(key) or None
        self._cache[cache_key] = (value, now + self._ttl)
        return value

    async def _load_from_db(self, key: str, *, user_id: int) -> str | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(ServiceCredential).where(
                    # user_id predicate is a deliberate IDOR guard -- see
                    # CLAUDE.md operating rule 12. Do not remove it.
                    ServiceCredential.user_id == user_id,
                    ServiceCredential.credential_key == key,
                )
            )
            if row is None:
                return None
            try:
                return decrypt_secret(row.encrypted_value)
            except InvalidEncryptedSecretError:
                # Key rotated without a backfill, or ciphertext corrupted.
                # Fall back to env rather than hard-failing the request path.
                logger.warning(
                    "credential_decrypt_failed",
                    extra={"credential_key": key, "user_id": user_id},
                )
                return None

    async def set_credential(self, *, user_id: int, key: str, value: str) -> str | None:
        """Upsert an encrypted credential. Returns the display hint."""
        if not is_ui_managed(key):
            raise UnknownCredentialError(f"{key} is not a UI-manageable credential")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Credential value cannot be empty")

        encrypted = encrypt_secret(cleaned)
        hint = _hint_for(cleaned)
        async with self._db.session() as session:
            row = await session.scalar(
                select(ServiceCredential).where(
                    ServiceCredential.user_id == user_id,
                    ServiceCredential.credential_key == key,
                )
            )
            if row is None:
                session.add(
                    ServiceCredential(
                        user_id=user_id,
                        credential_key=key,
                        encrypted_value=encrypted,
                        hint=hint,
                    )
                )
            else:
                row.encrypted_value = encrypted
                row.hint = hint
            await session.commit()

        self._cache.pop((user_id, key), None)
        logger.info("credential_set", extra={"credential_key": key, "user_id": user_id})
        return hint

    async def delete_credential(self, *, user_id: int, key: str) -> bool:
        """Remove a stored credential. Returns whether a row was deleted."""
        if not is_ui_managed(key):
            raise UnknownCredentialError(f"{key} is not a UI-manageable credential")
        async with self._db.session() as session:
            result = await session.execute(
                delete(ServiceCredential).where(
                    ServiceCredential.user_id == user_id,
                    ServiceCredential.credential_key == key,
                )
            )
            await session.commit()
        deleted = bool(cast("CursorResult[Any]", result).rowcount)
        self._cache.pop((user_id, key), None)
        if deleted:
            logger.info("credential_deleted", extra={"credential_key": key, "user_id": user_id})
        return deleted

    async def list_status(self, *, user_id: int) -> list[CredentialStatus]:
        """Return catalog entries with presence flags -- never the values."""
        async with self._db.session() as session:
            rows = (
                await session.scalars(
                    select(ServiceCredential).where(ServiceCredential.user_id == user_id)
                )
            ).all()
        stored = {row.credential_key: row for row in rows}

        return [
            CredentialStatus(
                key=spec.key,
                label=spec.label,
                group=spec.group,
                help_url=spec.help_url,
                configured_in_db=spec.key in stored,
                configured_in_env=bool(os.environ.get(spec.key)),
                hint=stored[spec.key].hint if spec.key in stored else None,
            )
            for spec in CATALOG.values()
        ]
