"""Tests for GitHub OAuth Device Flow endpoints.

US-019: POST /v1/auth/github/device/start
        POST /v1/auth/github/device/poll

All 9 cases from the PRD spec.
Requires TEST_DATABASE_URL (skipped otherwise — same pattern as PAT tests).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import fakeredis.aioredis as fakeredis
import pytest_asyncio
import respx

if TYPE_CHECKING:
    import pytest
from cryptography.fernet import Fernet
from httpx import Response

from app.api.routers.auth.tokens import create_access_token
from app.security.token_crypto import reset_key_cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_ID = 555_000_099
_OTHER_USER_ID = 555_000_100
_FERNET_KEY = Fernet.generate_key().decode()

_DEVICE_CODE = "A" * 20  # min_length=20
_USER_CODE = "ABCD-1234"
_VERIFICATION_URI = "https://github.com/login/device"
_EXPIRES_IN = 900
_INTERVAL = 5

_GH_DEVICE_START_PAYLOAD = {
    "device_code": _DEVICE_CODE,
    "user_code": _USER_CODE,
    "verification_uri": _VERIFICATION_URI,
    "expires_in": _EXPIRES_IN,
    "interval": _INTERVAL,
}

_GH_USER_PAYLOAD = {
    "id": 42,
    "login": "device-flow-user",
    "name": "Device Flow User",
    "email": None,
    "type": "User",
}

_ACCESS_TOKEN = "gho_fake_oauth_access_token_device"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[misc]
    """Set required env vars and reset caches for every test in this module."""
    monkeypatch.setenv("ALLOWED_USER_IDS", f"{_USER_ID},{_OTHER_USER_ID}")
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", _FERNET_KEY)
    monkeypatch.setenv("GITHUB_OAUTH_APP_CLIENT_ID", "Iv1.fake_client_id")
    monkeypatch.setenv("GITHUB_OAUTH_APP_CLIENT_SECRET", "fake_client_secret_abc123")
    reset_key_cache()
    yield
    reset_key_cache()


@pytest_asyncio.fixture
async def gh_user(db: Any, user_factory: Any) -> Any:
    return await user_factory(telegram_user_id=_USER_ID, username="device_flow_test_user")


@pytest_asyncio.fixture
async def other_user(db: Any, user_factory: Any) -> Any:
    return await user_factory(telegram_user_id=_OTHER_USER_ID, username="other_user")


def _auth_headers(user_id: int = _USER_ID) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def fake_redis() -> fakeredis.FakeRedis:
    """In-memory Redis compatible with the asyncio interface."""
    return fakeredis.FakeRedis()


@pytest_asyncio.fixture
def client_no_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    """TestClient that does NOT require a live Postgres database.

    Used by tests whose endpoint logic short-circuits before any DB access
    (Redis missing, CSRF mismatch, rate-limit).  All required env vars for
    load_config() to succeed are set here — DATABASE_URL is the placeholder
    value the global conftest uses so middleware (rate_limit etc.) can boot.
    """
    import importlib

    try:
        from fastapi.testclient import TestClient
    except ImportError:
        from starlette.testclient import TestClient

    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-at-least-32-chars-long-string")
    monkeypatch.setenv("REDIS_ENABLED", "0")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
    )

    from app.config import clear_config_cache

    clear_config_cache()

    import app.api.main

    importlib.reload(app.api.main)
    from app.api.main import app as _app

    try:
        from app.api import middleware as _mw

        _mw._local_rate_limits.clear()
        _mw._cfg_holder[0] = None  # reset cached config so it picks up new env vars
    except Exception:
        pass

    return TestClient(_app)


def _make_redis_state(
    user_id: int = _USER_ID,
    expires_offset: int = 900,
    last_poll_at: int = 0,
    interval: int = 5,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "expires_at": int(time.time()) + expires_offset,
        "last_poll_at": last_poll_at,
        "interval": interval,
    }


def _inject_redis(test_client: Any, fake_redis_instance: fakeredis.FakeRedis) -> None:
    """Set fake_redis on app.state so _get_redis_or_503 returns it."""
    test_client.app.state.redis = fake_redis_instance


def _clear_redis(test_client: Any) -> None:
    """Remove injected redis from app.state."""
    if hasattr(test_client.app.state, "redis"):
        del test_client.app.state.redis


@contextmanager
def _stub_use_case(test_client: Any):
    """Override _get_use_case via FastAPI dependency_overrides so tests that
    short-circuit before validate_and_store don't need a live DB."""
    from app.api.routers.auth.github import _get_use_case

    stub = MagicMock()
    test_client.app.dependency_overrides[_get_use_case] = lambda: stub
    try:
        yield stub
    finally:
        test_client.app.dependency_overrides.pop(_get_use_case, None)


# ---------------------------------------------------------------------------
# 1. device/start returns user_code and stores Redis key
# ---------------------------------------------------------------------------


async def test_device_start_returns_user_code(
    client: Any, db: Any, gh_user: Any, fake_redis: fakeredis.FakeRedis
) -> None:
    """Happy path: GitHub returns device_code payload; Redis key is written."""
    _inject_redis(client, fake_redis)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://github.com/login/device/code").mock(
                return_value=Response(200, json=_GH_DEVICE_START_PAYLOAD)
            )
            resp = client.post(
                "/v1/auth/github/device/start",
                headers=_auth_headers(),
            )
    finally:
        _clear_redis(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_code"] == _USER_CODE
    assert body["verification_uri"] == _VERIFICATION_URI
    assert body["device_code"] == _DEVICE_CODE
    assert body["interval"] == _INTERVAL
    assert body["expires_in"] == _EXPIRES_IN

    # Redis entry must exist and be bound to _USER_ID
    raw = await fake_redis.get(f"gh:device:{_DEVICE_CODE}")
    assert raw is not None
    state = json.loads(raw)
    assert int(state["user_id"]) == _USER_ID
    assert state["last_poll_at"] == 0


# ---------------------------------------------------------------------------
# 2. device/start returns 503 when oauth is unconfigured
# ---------------------------------------------------------------------------


async def test_device_start_returns_503_when_oauth_unconfigured(
    client: Any,
    db: Any,
    gh_user: Any,
    fake_redis: fakeredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_OAUTH_APP_CLIENT_ID", raising=False)
    from app.config import clear_config_cache

    clear_config_cache()

    _inject_redis(client, fake_redis)
    try:
        resp = client.post("/v1/auth/github/device/start", headers=_auth_headers())
    finally:
        _clear_redis(client)

    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "github_token_exchange_failed"
    assert body["error"]["details"]["error"] == "oauth_not_configured"
    assert body["error"]["correlation_id"]


# ---------------------------------------------------------------------------
# 3. device/start returns 503 when Redis is unavailable
# ---------------------------------------------------------------------------


async def test_device_start_returns_503_when_redis_unavailable(
    client_no_db: Any,
) -> None:
    """_get_redis_or_503 raises 503 when app.state.redis is None.

    No DB required: 503 fires before any DB access (JWT decode is stateless).
    """
    # app.state.redis is not set — _get_redis_or_503 returns 503
    resp = client_no_db.post("/v1/auth/github/device/start", headers=_auth_headers())
    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "github_token_exchange_failed"
    assert body["error"]["details"]["error"] == "redis_not_configured"
    assert body["error"]["correlation_id"]


async def test_device_start_github_rate_limit_returns_standard_envelope(
    client_no_db: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    _inject_redis(client_no_db, fake_redis)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://github.com/login/device/code").mock(
                return_value=Response(429, headers={"Retry-After": "17"})
            )
            resp = client_no_db.post("/v1/auth/github/device/start", headers=_auth_headers())
    finally:
        _clear_redis(client_no_db)

    assert resp.status_code == 429
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "github_oauth_rate_limited"
    assert body["error"]["retryable"] is True
    assert body["error"]["retry_after"] == 17
    assert body["error"]["correlation_id"]


# ---------------------------------------------------------------------------
# 4. device/poll with authorization_pending → status='pending'
# ---------------------------------------------------------------------------


async def test_device_poll_pending(
    client: Any,
    db: Any,
    gh_user: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    await fake_redis.set(
        f"gh:device:{_DEVICE_CODE}",
        json.dumps(_make_redis_state()),
        ex=900,
    )

    _inject_redis(client, fake_redis)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://github.com/login/oauth/access_token").mock(
                return_value=Response(200, json={"error": "authorization_pending"})
            )
            resp = client.post(
                "/v1/auth/github/device/poll",
                json={"device_code": _DEVICE_CODE},
                headers=_auth_headers(),
            )
    finally:
        _clear_redis(client)

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# 5. device/poll ok → validate_and_store called, status='ok'
# ---------------------------------------------------------------------------


async def test_device_poll_ok_stores_token(
    client: Any,
    db: Any,
    gh_user: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    await fake_redis.set(
        f"gh:device:{_DEVICE_CODE}",
        json.dumps(_make_redis_state()),
        ex=900,
    )

    _inject_redis(client, fake_redis)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://github.com/login/oauth/access_token").mock(
                return_value=Response(
                    200, json={"access_token": _ACCESS_TOKEN, "token_type": "bearer", "scope": ""}
                )
            )
            mock.get("https://api.github.com/user").mock(
                return_value=Response(
                    200,
                    json=_GH_USER_PAYLOAD,
                    headers={"X-GitHub-OAuthScopes": "repo, read:user"},
                )
            )
            resp = client.post(
                "/v1/auth/github/device/poll",
                json={"device_code": _DEVICE_CODE},
                headers=_auth_headers(),
            )
    finally:
        _clear_redis(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["login"] == "device-flow-user"
    assert body["github_user_id"] == 42
    assert body["auth_method"] == "oauth_device"
    assert body["integration_status"] == "active"

    # Redis entry must be deleted after success
    raw = await fake_redis.get(f"gh:device:{_DEVICE_CODE}")
    assert raw is None


# ---------------------------------------------------------------------------
# 6. device/poll with unknown device_code → status='expired'
# ---------------------------------------------------------------------------


async def test_device_poll_unknown_device_code_returns_expired(
    client_no_db: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    # Do NOT seed Redis — no entry exists; returns 'expired' before any DB access.
    _inject_redis(client_no_db, fake_redis)
    try:
        with _stub_use_case(client_no_db):
            resp = client_no_db.post(
                "/v1/auth/github/device/poll",
                json={"device_code": _DEVICE_CODE},
                headers=_auth_headers(),
            )
    finally:
        _clear_redis(client_no_db)

    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"


# ---------------------------------------------------------------------------
# 7. device/poll with device_code owned by a different user → status='expired'
# ---------------------------------------------------------------------------


async def test_device_poll_csrf_other_user_returns_expired(
    client_no_db: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    # Seed Redis as if _OTHER_USER_ID started the flow
    await fake_redis.set(
        f"gh:device:{_DEVICE_CODE}",
        json.dumps(_make_redis_state(user_id=_OTHER_USER_ID)),
        ex=900,
    )

    # Poll arrives with _USER_ID's JWT → user_id mismatch → 'expired'
    # No DB access: the CSRF check short-circuits before validate_and_store.
    _inject_redis(client_no_db, fake_redis)
    try:
        with _stub_use_case(client_no_db):
            resp = client_no_db.post(
                "/v1/auth/github/device/poll",
                json={"device_code": _DEVICE_CODE},
                headers=_auth_headers(_USER_ID),
            )
    finally:
        _clear_redis(client_no_db)

    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"


# ---------------------------------------------------------------------------
# 8. device/poll rate-limited (last_poll_at too recent) → status='slow_down'
#    GitHub must NOT be called
# ---------------------------------------------------------------------------


async def test_device_poll_slow_down_when_polled_too_fast(
    client_no_db: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    # last_poll_at = now (just polled) — rate-limit fires before GitHub call
    recent_poll = int(time.time())
    await fake_redis.set(
        f"gh:device:{_DEVICE_CODE}",
        json.dumps(_make_redis_state(last_poll_at=recent_poll, interval=5)),
        ex=900,
    )

    _inject_redis(client_no_db, fake_redis)
    try:
        with _stub_use_case(client_no_db):
            with respx.mock(assert_all_called=True):
                # assert_all_called=True with zero routes registered: any GitHub call
                # would be unmatched and raise ConnectError, proving none were made.
                resp = client_no_db.post(
                    "/v1/auth/github/device/poll",
                    json={"device_code": _DEVICE_CODE},
                    headers=_auth_headers(),
                )
    finally:
        _clear_redis(client_no_db)

    assert resp.status_code == 200
    assert resp.json()["status"] == "slow_down"


# ---------------------------------------------------------------------------
# 9. device/poll with expired_token from GitHub → Redis deleted, status='expired'
# ---------------------------------------------------------------------------


async def test_device_poll_expired(
    client: Any,
    db: Any,
    gh_user: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    await fake_redis.set(
        f"gh:device:{_DEVICE_CODE}",
        json.dumps(_make_redis_state()),
        ex=900,
    )

    _inject_redis(client, fake_redis)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://github.com/login/oauth/access_token").mock(
                return_value=Response(200, json={"error": "expired_token"})
            )
            resp = client.post(
                "/v1/auth/github/device/poll",
                json={"device_code": _DEVICE_CODE},
                headers=_auth_headers(),
            )
    finally:
        _clear_redis(client)

    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"

    # Redis key must be gone
    raw = await fake_redis.get(f"gh:device:{_DEVICE_CODE}")
    assert raw is None


# ---------------------------------------------------------------------------
# 10. device/poll: insufficient-scope token → 422
# ---------------------------------------------------------------------------


async def test_device_poll_insufficient_scope_returns_422(
    client: Any,
    db: Any,
    gh_user: Any,
    fake_redis: fakeredis.FakeRedis,
) -> None:
    """OAuth token missing repo scope → 422, device_code consumed from Redis."""
    await fake_redis.set(
        f"gh:device:{_DEVICE_CODE}",
        json.dumps(_make_redis_state()),
        ex=900,
    )

    _inject_redis(client, fake_redis)
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://github.com/login/oauth/access_token").mock(
                return_value=Response(
                    200, json={"access_token": _ACCESS_TOKEN, "token_type": "bearer", "scope": ""}
                )
            )
            mock.get("https://api.github.com/user").mock(
                return_value=Response(
                    200,
                    json=_GH_USER_PAYLOAD,
                    headers={"X-GitHub-OAuthScopes": "read:user, public_repo"},
                )
            )
            resp = client.post(
                "/v1/auth/github/device/poll",
                json={"device_code": _DEVICE_CODE},
                headers=_auth_headers(),
            )
    finally:
        _clear_redis(client)

    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "github_token_invalid"
    assert "missing required scopes" in body["error"]["message"]
    assert "repo" in body["error"]["message"]
    assert body["error"]["correlation_id"]

    # Redis key must be consumed (deleted before validate_and_store)
    raw = await fake_redis.get(f"gh:device:{_DEVICE_CODE}")
    assert raw is None
