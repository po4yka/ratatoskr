"""Encrypted storage for Playwright browser sessions used by AI account backup.

Reuses the existing ``user_browser_sessions`` table (one row per
``(user_id, domain)``) and the project-wide Fernet helpers. The stored blob is
the full Playwright ``storage_state`` dict (cookies + localStorage) serialized
to JSON and encrypted at rest. Plaintext never lands on disk and is never logged.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.core.logging_utils import get_logger
from app.db.models.ai_backup import AiBackupService
from app.db.models.webwright import UserBrowserSession
from app.security.secret_crypto import (
    InvalidEncryptedSecretError,
    decrypt_secret,
    encrypt_secret,
)

if TYPE_CHECKING:
    from app.db.session import Database

logger = get_logger(__name__)

# Stable per-service domains — never derived from user input.
_SERVICE_DOMAIN: dict[AiBackupService, str] = {
    AiBackupService.CHATGPT: "chatgpt.com",
    AiBackupService.CLAUDE: "claude.ai",
}

_SESSION_COOKIE_NAMES: dict[AiBackupService, tuple[str, ...]] = {
    AiBackupService.CHATGPT: ("__Secure-next-auth.session-token",),
    AiBackupService.CLAUDE: ("sessionKey",),
}


def domain_for_service(service: AiBackupService) -> str:
    """Return the canonical (never user-derived) domain for an AI backup service."""
    return _SERVICE_DOMAIN[service]


def validate_storage_state_shape(obj: object) -> None:
    """Raise ``ValueError`` unless ``obj`` is a Playwright storage_state dict.

    Minimum contract: a mapping with a ``cookies`` key whose value is a list.
    Kept intentionally loose so future Playwright additions (e.g. ``origins``)
    do not break ingestion.
    """
    if not isinstance(obj, dict):
        raise ValueError("storage_state must be a JSON object")
    cookies = obj.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("storage_state must contain a 'cookies' list")


def validate_storage_state(
    service: AiBackupService,
    obj: object,
    *,
    now_timestamp: float | None = None,
) -> None:
    """Validate that a storage state contains a usable service session cookie."""
    validate_storage_state_shape(obj)
    assert isinstance(obj, dict)  # narrowed by validate_storage_state_shape
    cookies = obj["cookies"]
    assert isinstance(cookies, list)
    now = time.time() if now_timestamp is None else now_timestamp
    expected_domain = domain_for_service(service)
    expected_names = _SESSION_COOKIE_NAMES[service]

    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        if not isinstance(name, str) or not any(
            name == expected or name.startswith(f"{expected}.") for expected in expected_names
        ):
            continue
        domain = cookie.get("domain")
        if not isinstance(domain, str) or domain.lstrip(".").casefold() != expected_domain:
            continue
        value = cookie.get("value")
        if not isinstance(value, str) or not value:
            continue
        expires = cookie.get("expires", -1)
        if not isinstance(expires, (int, float)) or isinstance(expires, bool):
            continue
        if expires > 0 and expires <= now:
            continue
        return

    raise ValueError(
        f"storage_state has no usable {service.value} session cookie for {expected_domain}"
    )


class AiBackupSessionStore:
    """Load and persist encrypted Playwright sessions for AI backup domains."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self, user_id: int, service: AiBackupService) -> dict | None:
        """Return the decrypted storage_state dict, or ``None`` when absent.

        Never logs the returned value. Raises ``InvalidEncryptedSecretError`` if
        the stored ciphertext cannot be decrypted (e.g. key rotated without a
        backfill) so the caller can decide to treat it as absent.
        """
        domain = _SERVICE_DOMAIN[service]
        async with self._db.session() as session:
            row = await session.scalar(
                select(UserBrowserSession).where(
                    UserBrowserSession.user_id == user_id,
                    UserBrowserSession.domain == domain,
                )
            )
        if row is None:
            return None
        try:
            plaintext = decrypt_secret(row.encrypted_cookies)
            storage_state = json.loads(plaintext)
            validate_storage_state(service, storage_state)
        except (InvalidEncryptedSecretError, json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "ai_backup_session_decrypt_failed",
                extra={"user_id": user_id, "domain": domain},
            )
            raise InvalidEncryptedSecretError("Stored browser session is invalid") from exc
        return storage_state

    async def save(self, user_id: int, service: AiBackupService, storage_state: dict) -> None:
        """Validate, encrypt, and upsert the storage_state for ``(user, service)``.

        Validates shape before any DB write. Never logs the storage_state value.
        """
        validate_storage_state(service, storage_state)
        domain = _SERVICE_DOMAIN[service]
        encrypted = encrypt_secret(json.dumps(storage_state, ensure_ascii=False))
        async with self._db.transaction() as session:
            row = await session.scalar(
                select(UserBrowserSession).where(
                    UserBrowserSession.user_id == user_id,
                    UserBrowserSession.domain == domain,
                )
            )
            if row is None:
                session.add(
                    UserBrowserSession(
                        user_id=user_id,
                        domain=domain,
                        encrypted_cookies=encrypted,
                        note="ai_backup",
                    )
                )
            else:
                row.encrypted_cookies = encrypted

    async def delete(self, user_id: int, service: AiBackupService) -> None:
        """Idempotently delete the encrypted session for ``(user, service)``."""
        domain = _SERVICE_DOMAIN[service]
        async with self._db.transaction() as session:
            await session.execute(
                delete(UserBrowserSession).where(
                    UserBrowserSession.user_id == user_id,
                    UserBrowserSession.domain == domain,
                )
            )

    async def refresh(self, user_id: int, service: AiBackupService, storage_state: dict) -> bool:
        """Update a still-present session without recreating a revoked row.

        Returns ``False`` when the row disappeared during a running backup.
        This prevents the run's final cookie refresh from undoing an explicit
        owner revoke.
        """
        validate_storage_state(service, storage_state)
        domain = _SERVICE_DOMAIN[service]
        encrypted = encrypt_secret(json.dumps(storage_state, ensure_ascii=False))
        async with self._db.transaction() as session:
            row = await session.scalar(
                select(UserBrowserSession).where(
                    UserBrowserSession.user_id == user_id,
                    UserBrowserSession.domain == domain,
                )
            )
            if row is None:
                return False
            row.encrypted_cookies = encrypted
        return True


__all__ = [
    "AiBackupSessionStore",
    "domain_for_service",
    "validate_storage_state",
    "validate_storage_state_shape",
]
