"""Config load enforces the pepper domain-separation invariant.

JWT_SECRET_KEY, SECRET_LOGIN_PEPPER, and CREDENTIALS_LOGIN_PEPPER each key a
separate security domain (JWT signing, secret-key hashing, password hashing) and
must be independent. The field docs required this but nothing enforced it until
Settings._ensure_auth_secret_domain_separation. Reusing one value across two
domains must now fail at config load; distinct or unset values must pass.
"""

from __future__ import annotations

import os
import unittest.mock

import pytest

from app.config.settings import Settings, clear_config_cache
from tests._config_env import MODEL_SELECTION_ENV

# A >=32-char value reused across domains to trip the check. Never equals any of
# the distinct fixtures below.
_SHARED = "Z" * 48

_MINIMAL_ENV = {
    **MODEL_SELECTION_ENV,
    "API_ID": "12345",
    "API_HASH": "abc123",
    "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ALLOWED_USER_IDS": "999",
    "ALLOWED_CLIENT_IDS": "test-client",
    "OPENROUTER_API_KEY": "sk-test",
    "FIRECRAWL_API_KEY": "",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "GITHUB_TOKEN_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    # Peppers/JWT are secret-marked so YAML can never set them, but pin the
    # loader at a missing path so no repo ratatoskr.yaml perturbs the load.
    "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
}


def _build(**overrides: str) -> Settings:
    with unittest.mock.patch.dict(os.environ, {**_MINIMAL_ENV, **overrides}, clear=True):
        clear_config_cache()
        try:
            return Settings(allow_stub_telegram=True)
        finally:
            clear_config_cache()


def test_jwt_and_secret_pepper_must_differ() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY and SECRET_LOGIN_PEPPER"):
        _build(JWT_SECRET_KEY=_SHARED, SECRET_LOGIN_PEPPER=_SHARED)


def test_jwt_and_credentials_pepper_must_differ() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY and CREDENTIALS_LOGIN_PEPPER"):
        _build(JWT_SECRET_KEY=_SHARED, CREDENTIALS_LOGIN_PEPPER=_SHARED)


def test_secret_pepper_and_credentials_pepper_must_differ() -> None:
    # JWT_SECRET_KEY left unset so only the two peppers collide.
    with pytest.raises(RuntimeError, match="SECRET_LOGIN_PEPPER and CREDENTIALS_LOGIN_PEPPER"):
        _build(SECRET_LOGIN_PEPPER=_SHARED, CREDENTIALS_LOGIN_PEPPER=_SHARED)


def test_reuse_survives_surrounding_whitespace() -> None:
    # The validated (stripped) values are compared, so a copy-paste that differs
    # only by trailing whitespace is still the same effective secret.
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY and SECRET_LOGIN_PEPPER"):
        _build(JWT_SECRET_KEY=_SHARED, SECRET_LOGIN_PEPPER=f"  {_SHARED}  ")


def test_distinct_secrets_load_cleanly() -> None:
    settings = _build(
        JWT_SECRET_KEY="J" * 48,
        SECRET_LOGIN_PEPPER="S" * 48,
        CREDENTIALS_LOGIN_PEPPER="C" * 48,
    )
    assert settings.runtime.jwt_secret_key == "J" * 48
    assert settings.auth.secret_pepper == "S" * 48
    assert settings.auth.credentials_pepper == "C" * 48


def test_unset_peppers_are_not_compared() -> None:
    # Only JWT set; both peppers unset -> nothing to collide, loads cleanly.
    settings = _build(JWT_SECRET_KEY=_SHARED)
    assert settings.auth.secret_pepper is None
    assert settings.auth.credentials_pepper is None
