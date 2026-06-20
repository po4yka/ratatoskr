"""Envelope consistency tests for API routes with custom error handling."""

from __future__ import annotations

import importlib
import json
import time
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis as fakeredis
import pytest
import pytest_asyncio
import respx
from cryptography.fernet import Fernet
from httpx import Response

from app.api.routers.auth.tokens import create_access_token
from app.application.exceptions.github import InsufficientScopeError, InvalidGitHubTokenError
from app.security.token_crypto import reset_key_cache

_USER_ID = 555_000_700
_DEVICE_CODE = "D" * 20
_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def github_auth_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_USER_ID))
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-at-least-32-chars-long-string")
    monkeypatch.setenv("REDIS_ENABLED", "0")
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", _FERNET_KEY)
    monkeypatch.setenv("GITHUB_OAUTH_APP_CLIENT_ID", "Iv1.fake_client_id")
    monkeypatch.setenv("GITHUB_OAUTH_APP_CLIENT_SECRET", "fake_client_secret")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
    )

    from app.config import clear_config_cache

    clear_config_cache()
    reset_key_cache()

    try:
        from fastapi.testclient import TestClient
    except ImportError:
        from starlette.testclient import TestClient

    import app.api.main

    importlib.reload(app.api.main)
    from app.api.main import app
    from app.api.routers.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": _USER_ID,
        "username": "github-envelope-user",
        "client_id": "test",
    }

    try:
        from app.api import middleware as _middleware

        _middleware._local_rate_limits.clear()
        _middleware._cfg_holder[0] = None
    except Exception:
        pass

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        if hasattr(app.state, "redis"):
            del app.state.redis
        clear_config_cache()
        reset_key_cache()


@pytest_asyncio.fixture
async def fake_redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


def _auth_headers() -> dict[str, str]:
    token = create_access_token(_USER_ID, client_id="test")
    return {"Authorization": f"Bearer {token}"}


def _assert_error_envelope(
    response: Any,
    *,
    status_code: int,
    code: str,
    retryable: bool | None = None,
) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["success"] is False
    assert "data" not in body
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["error"]["errorType"]
    assert body["error"]["correlation_id"]
    assert body["meta"]["api_version"]
    if retryable is not None:
        assert body["error"]["retryable"] is retryable
    return body


class _UseCase:
    def __init__(
        self,
        *,
        validate_exc: Exception | None = None,
        connected: bool = True,
    ) -> None:
        self._validate_exc = validate_exc
        self._connected = connected

    async def validate_and_store(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._validate_exc is not None:
            raise self._validate_exc
        integration = SimpleNamespace(
            github_login="octo",
            github_user_id=1,
        )
        return integration, []

    async def get_status(self, _user_id: int) -> Any:
        return SimpleNamespace(is_connected=self._connected)


def _override_use_case(client: Any, use_case: _UseCase) -> None:
    from app.api.routers.auth.github import _get_use_case

    client.app.dependency_overrides[_get_use_case] = lambda: use_case


def _inject_redis(client: Any, redis: fakeredis.FakeRedis) -> None:
    client.app.state.redis = redis


async def _seed_device_state(redis: fakeredis.FakeRedis) -> None:
    state = {
        "user_id": _USER_ID,
        "expires_at": int(time.time()) + 900,
        "last_poll_at": 0,
        "interval": 5,
    }
    await redis.set(f"gh:device:{_DEVICE_CODE}", json.dumps(state), ex=900)


def test_github_pat_validation_error_uses_standard_envelope(github_auth_client: Any) -> None:
    response = github_auth_client.post(
        "/v1/auth/github/pat",
        json={"token": "short"},
        headers=_auth_headers(),
    )

    _assert_error_envelope(response, status_code=422, code="VALIDATION_ERROR", retryable=False)


@pytest.mark.parametrize(
    ("exc", "status_code"),
    [
        (InvalidGitHubTokenError("bad token"), 400),
        (InsufficientScopeError(["repo"]), 422),
    ],
)
def test_github_pat_use_case_errors_use_standard_envelope(
    github_auth_client: Any,
    exc: Exception,
    status_code: int,
) -> None:
    _override_use_case(github_auth_client, _UseCase(validate_exc=exc))

    response = github_auth_client.post(
        "/v1/auth/github/pat",
        json={"token": "ghp_invalid_bad_token_xyz"},
        headers=_auth_headers(),
    )

    _assert_error_envelope(response, status_code=status_code, code="github_token_invalid")


def test_github_sync_missing_integration_uses_standard_envelope(github_auth_client: Any) -> None:
    _override_use_case(github_auth_client, _UseCase(connected=False))

    response = github_auth_client.post("/v1/auth/github/sync", headers=_auth_headers())

    _assert_error_envelope(response, status_code=404, code="github_token_invalid")


def test_github_device_start_missing_redis_uses_standard_envelope(
    github_auth_client: Any,
) -> None:
    response = github_auth_client.post(
        "/v1/auth/github/device/start",
        headers=_auth_headers(),
    )

    _assert_error_envelope(
        response,
        status_code=503,
        code="github_token_exchange_failed",
        retryable=True,
    )


def test_github_device_start_unconfigured_oauth_uses_standard_envelope(
    github_auth_client: Any,
    fake_redis: fakeredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        github=SimpleNamespace(oauth_app_client_id=None, oauth_app_client_secret=None)
    )
    monkeypatch.setattr("app.config.settings.load_config", lambda **_kwargs: config)
    _inject_redis(github_auth_client, fake_redis)

    response = github_auth_client.post(
        "/v1/auth/github/device/start",
        headers=_auth_headers(),
    )

    _assert_error_envelope(
        response,
        status_code=503,
        code="github_token_exchange_failed",
        retryable=True,
    )


@pytest.mark.parametrize(
    ("provider_response", "status_code", "code", "retryable"),
    [
        (Response(429, headers={"Retry-After": "17"}), 429, "github_oauth_rate_limited", True),
        (
            Response(500, json={"message": "server error"}),
            503,
            "github_token_exchange_failed",
            True,
        ),
    ],
)
def test_github_device_start_provider_errors_use_standard_envelope(
    github_auth_client: Any,
    fake_redis: fakeredis.FakeRedis,
    provider_response: Response,
    status_code: int,
    code: str,
    retryable: bool,
) -> None:
    _inject_redis(github_auth_client, fake_redis)

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://github.com/login/device/code").mock(return_value=provider_response)
        response = github_auth_client.post(
            "/v1/auth/github/device/start",
            headers=_auth_headers(),
        )

    body = _assert_error_envelope(
        response,
        status_code=status_code,
        code=code,
        retryable=retryable,
    )
    if code == "github_oauth_rate_limited":
        assert body["error"]["retry_after"] == 17


def test_github_device_poll_validation_error_uses_standard_envelope(
    github_auth_client: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    _inject_redis(github_auth_client, fake_redis)

    response = github_auth_client.post(
        "/v1/auth/github/device/poll",
        json={"device_code": "short"},
        headers=_auth_headers(),
    )

    _assert_error_envelope(response, status_code=422, code="VALIDATION_ERROR", retryable=False)


def test_github_device_poll_missing_redis_uses_standard_envelope(
    github_auth_client: Any,
) -> None:
    response = github_auth_client.post(
        "/v1/auth/github/device/poll",
        json={"device_code": _DEVICE_CODE},
        headers=_auth_headers(),
    )

    _assert_error_envelope(
        response,
        status_code=503,
        code="github_token_exchange_failed",
        retryable=True,
    )


def test_github_device_poll_unconfigured_oauth_uses_standard_envelope(
    github_auth_client: Any,
    fake_redis: fakeredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        github=SimpleNamespace(
            oauth_app_client_id="Iv1.fake_client_id",
            oauth_app_client_secret=None,
        )
    )
    monkeypatch.setattr("app.config.settings.load_config", lambda **_kwargs: config)
    _inject_redis(github_auth_client, fake_redis)
    _override_use_case(github_auth_client, _UseCase())

    response = github_auth_client.post(
        "/v1/auth/github/device/poll",
        json={"device_code": _DEVICE_CODE},
        headers=_auth_headers(),
    )

    _assert_error_envelope(
        response,
        status_code=503,
        code="github_token_exchange_failed",
        retryable=True,
    )


@pytest.mark.parametrize(
    ("provider_response", "status_code", "code", "retryable"),
    [
        (Response(429, headers={"Retry-After": "19"}), 429, "github_oauth_rate_limited", True),
        (
            Response(500, json={"message": "server error"}),
            503,
            "github_token_exchange_failed",
            True,
        ),
    ],
)
async def test_github_device_poll_provider_errors_use_standard_envelope(
    github_auth_client: Any,
    fake_redis: fakeredis.FakeRedis,
    provider_response: Response,
    status_code: int,
    code: str,
    retryable: bool,
) -> None:
    await _seed_device_state(fake_redis)
    _inject_redis(github_auth_client, fake_redis)
    _override_use_case(github_auth_client, _UseCase())

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=provider_response
        )
        response = github_auth_client.post(
            "/v1/auth/github/device/poll",
            json={"device_code": _DEVICE_CODE},
            headers=_auth_headers(),
        )

    body = _assert_error_envelope(
        response,
        status_code=status_code,
        code=code,
        retryable=retryable,
    )
    if code == "github_oauth_rate_limited":
        assert body["error"]["retry_after"] == 19


@pytest.mark.parametrize(
    ("exc", "status_code"),
    [
        (InvalidGitHubTokenError("bad token"), 400),
        (InsufficientScopeError(["repo"]), 422),
    ],
)
async def test_github_device_poll_use_case_errors_use_standard_envelope(
    github_auth_client: Any,
    fake_redis: fakeredis.FakeRedis,
    exc: Exception,
    status_code: int,
) -> None:
    await _seed_device_state(fake_redis)
    _inject_redis(github_auth_client, fake_redis)
    _override_use_case(github_auth_client, _UseCase(validate_exc=exc))

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(
                200,
                json={"access_token": "gho_fake_access_token", "token_type": "bearer"},
            )
        )
        response = github_auth_client.post(
            "/v1/auth/github/device/poll",
            json={"device_code": _DEVICE_CODE},
            headers=_auth_headers(),
        )

    _assert_error_envelope(response, status_code=status_code, code="github_token_invalid")
