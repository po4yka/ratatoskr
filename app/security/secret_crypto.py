"""Fernet symmetric encryption for at-rest integration secrets.

This is the provider-neutral API for encrypting stored access tokens and refresh tokens. The current key material is still loaded from the GitHub token encryption settings so existing GitHub rows and rotation workflows remain backward-compatible.

Key rotation follows the existing MultiFernet flow:
1. Set the new key as ``GITHUB_TOKEN_ENCRYPTION_KEY``.
2. Move the old key to ``GITHUB_TOKEN_PREVIOUS_KEYS``.
3. Deploy; old ciphertext still decrypts and new writes use the new key.
4. Backfill existing ciphertext with the rotation CLI.
5. Remove old keys after the backfill is complete.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

__all__ = [
    "InvalidEncryptedSecretError",
    "MissingSecretEncryptionKeyError",
    "decrypt_secret",
    "encrypt_secret",
    "reset_secret_key_cache",
]


class MissingSecretEncryptionKeyError(RuntimeError):
    """Raised when the configured Fernet key is unset or malformed."""


class InvalidEncryptedSecretError(ValueError):
    """Raised when ciphertext cannot be decrypted."""


def _parse_previous_keys(raw: str | None) -> list[Fernet]:
    if not raw:
        return []
    result: list[Fernet] = []
    for i, part in enumerate(p.strip() for p in raw.split(",") if p.strip()):
        encoded = part.encode("utf-8")
        try:
            result.append(Fernet(encoded))
        except (ValueError, TypeError) as exc:
            raise MissingSecretEncryptionKeyError(
                f"GITHUB_TOKEN_PREVIOUS_KEYS[{i}] is malformed "
                f"(must be 32 url-safe base64 bytes). Underlying error: {exc}"
            ) from exc
    return result


@lru_cache(maxsize=1)
def _get_multi_fernet() -> MultiFernet:
    from app.config.settings import load_config

    settings = load_config(allow_stub_telegram=True)
    secret = settings.github.token_encryption_key
    if secret is None:
        raise MissingSecretEncryptionKeyError(
            "GITHUB_TOKEN_ENCRYPTION_KEY is not configured. "
            "Generate one with: python tools/scripts/generate_github_encryption_key.py "
            "and set it in your .env file."
        )
    raw_value = secret.get_secret_value()
    raw = raw_value.encode("utf-8") if isinstance(raw_value, str) else raw_value
    try:
        primary = Fernet(raw)
    except (ValueError, TypeError) as exc:
        raise MissingSecretEncryptionKeyError(
            "GITHUB_TOKEN_ENCRYPTION_KEY is malformed (must be 32 url-safe base64 bytes). "
            "Generate one with: python tools/scripts/generate_github_encryption_key.py. "
            f"Underlying error: {exc}"
        ) from exc

    prev_secret = settings.github.token_previous_keys
    prev_raw = prev_secret.get_secret_value() if prev_secret is not None else None
    previous = _parse_previous_keys(prev_raw)
    return MultiFernet([primary, *previous])


def encrypt_secret(plaintext: str) -> bytes:
    """Encrypt a non-empty secret string with the primary key."""
    if not plaintext:
        raise ValueError("Cannot encrypt empty plaintext")
    return _get_multi_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    """Decrypt previously encrypted ciphertext with the primary and previous keys."""
    try:
        return _get_multi_fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise InvalidEncryptedSecretError("Ciphertext could not be decrypted") from exc


def reset_secret_key_cache() -> None:
    """Clear cached key material and settings. Intended for tests."""
    _get_multi_fernet.cache_clear()
    from app.config.settings import clear_config_cache

    clear_config_cache()
