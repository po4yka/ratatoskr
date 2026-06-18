from __future__ import annotations

from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet

from app.security.secret_crypto import (
    InvalidEncryptedSecretError,
    MissingSecretEncryptionKeyError,
    decrypt_secret,
    encrypt_secret,
    reset_secret_key_cache,
)
from app.security.token_crypto import decrypt_token, encrypt_token


@pytest.fixture(autouse=True)
def _reset_cache() -> Generator[None]:
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


def test_secret_crypto_round_trip_reuses_configured_token_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    ciphertext = encrypt_secret("social-access-token")

    assert ciphertext != b"social-access-token"
    assert decrypt_secret(ciphertext) == "social-access-token"


def test_secret_crypto_is_backward_compatible_with_token_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    social_ciphertext = encrypt_secret("threads-token")
    github_ciphertext = encrypt_token("github-token")

    assert decrypt_token(social_ciphertext) == "threads-token"
    assert decrypt_secret(github_ciphertext) == "github-token"


def test_secret_crypto_uses_previous_keys_for_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_ciphertext = Fernet(old_key).encrypt(b"instagram-token")
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", new_key.decode("ascii"))
    monkeypatch.setenv("GITHUB_TOKEN_PREVIOUS_KEYS", old_key.decode("ascii"))

    assert decrypt_secret(old_ciphertext) == "instagram-token"


def test_secret_crypto_rejects_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN_ENCRYPTION_KEY", raising=False)

    with pytest.raises(MissingSecretEncryptionKeyError):
        encrypt_secret("x-token")


def test_secret_crypto_rejects_invalid_ciphertext(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    with pytest.raises(InvalidEncryptedSecretError):
        decrypt_secret(b"not-fernet")
