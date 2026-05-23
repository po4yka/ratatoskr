"""Backward-compatible GitHub token crypto facade.

Provider-neutral secret encryption lives in :mod:`app.security.secret_crypto`. This module preserves the existing GitHub integration import path and function names.
"""

from __future__ import annotations

from app.security.secret_crypto import (
    InvalidEncryptedSecretError,
    MissingSecretEncryptionKeyError,
    decrypt_secret,
    encrypt_secret,
    reset_secret_key_cache,
)

__all__ = [
    "InvalidEncryptedTokenError",
    "MissingEncryptionKeyError",
    "decrypt_token",
    "encrypt_token",
    "reset_key_cache",
]


class MissingEncryptionKeyError(MissingSecretEncryptionKeyError):
    """Raised when the GitHub-compatible token encryption key is unset or malformed."""


class InvalidEncryptedTokenError(InvalidEncryptedSecretError):
    """Raised when token ciphertext cannot be decrypted."""


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt a token string with the primary key. Returns Fernet ciphertext bytes."""
    try:
        return encrypt_secret(plaintext)
    except MissingSecretEncryptionKeyError as exc:
        raise MissingEncryptionKeyError(str(exc)) from exc


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt previously encrypted ciphertext. Tries primary key then all previous keys."""
    try:
        return decrypt_secret(ciphertext)
    except MissingSecretEncryptionKeyError as exc:
        raise MissingEncryptionKeyError(str(exc)) from exc
    except InvalidEncryptedSecretError as exc:
        raise InvalidEncryptedTokenError(str(exc)) from exc


def reset_key_cache() -> None:
    """Clear the cached MultiFernet instance and the settings config cache. For tests."""
    reset_secret_key_cache()
