import json

import pytest

# These tests require optional 'api' extras.
pytest.importorskip(
    "starlette", reason="Starlette not installed (install with: pip install .[api])"
)

import fakeredis.aioredis
from starlette.requests import Request
from starlette.responses import Response

from app.api import middleware
from app.api.routers.auth.tokens import create_access_token, create_token
from app.api.services.sync_service import SyncService
from app.config import ApiLimitsConfig, RedisConfig, SyncConfig
from app.config.deployment import DeploymentConfig
from app.infrastructure.redis import redis_key


class DummyCfg:
    def __init__(
        self,
        *,
        required: bool = False,
        limit: int = 5,
        window_seconds: int = 60,
        secret_login_limit: int = 20,
        credentials_login_limit: int = 5,
        aggregation_user_limit: int = 5,
        aggregation_client_limit: int = 20,
    ):
        self.redis = RedisConfig(enabled=True, required=required, prefix="test")
        self.api_limits = ApiLimitsConfig(
            window_seconds=window_seconds,
            cooldown_multiplier=1.0,
            max_concurrent=3,
            default_limit=limit,
            summaries_limit=limit,
            requests_limit=limit,
            search_limit=limit,
            auth_limit=limit,
            secret_login_limit=secret_login_limit,
            credentials_login_limit=credentials_login_limit,
            aggregation_create_user_limit=aggregation_user_limit,
            aggregation_create_client_limit=aggregation_client_limit,
        )
        self.sync = SyncConfig(expiry_hours=1, default_limit=100, min_limit=1, max_limit=500)
        # rate_limit_middleware reads cfg.deployment.is_production_mode when the
        # redis backend is unavailable; supply a default DeploymentConfig.
        self.deployment = DeploymentConfig()


def _auth_header(user_id: int, client_id: str) -> list[tuple[bytes, bytes]]:
    token = create_access_token(user_id, client_id=client_id)
    return [(b"authorization", f"Bearer {token}".encode())]


def _json_request(
    *,
    method: str,
    path: str,
    body: dict[str, object],
    host: str = "127.0.0.1",
) -> Request:
    raw = json.dumps(body).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode()),
            ],
            "client": (host, 0),
        },
        receive=receive,
    )


def test_auth_endpoints_resolve_to_route_specific_buckets() -> None:
    cases = {
        ("POST", "/v1/auth/secret-login"): "secret_login",
        ("POST", "/v1/auth/credentials-login"): "credentials_login",
        ("POST", "/v1/auth/telegram-login"): "telegram_login",
        ("POST", "/v1/auth/refresh"): "auth_refresh",
        ("POST", "/v1/auth/logout"): "auth_logout",
        ("POST", "/v1/auth/logout-all"): "auth_logout_all",
        ("GET", "/v1/auth/sessions"): "auth_sessions_list",
        ("DELETE", "/v1/auth/sessions/123"): "auth_session_delete",
        ("GET", "/v1/auth/me"): "auth_me",
        ("DELETE", "/v1/auth/me"): "auth_delete_account",
        ("POST", "/v1/auth/credentials/change-password"): "auth_credentials_change_password",
        ("GET", "/v1/auth/me/telegram"): "auth_telegram_status",
        ("POST", "/v1/auth/me/telegram/link"): "auth_telegram_link",
        ("POST", "/v1/auth/me/telegram/complete"): "auth_telegram_complete",
        ("DELETE", "/v1/auth/me/telegram"): "auth_telegram_unlink",
        ("POST", "/v1/auth/secret-keys"): "auth_secret_key_create",
        ("GET", "/v1/auth/secret-keys"): "auth_secret_key_list",
        ("POST", "/v1/auth/secret-keys/7/rotate"): "auth_secret_key_rotate",
        ("POST", "/v1/auth/secret-keys/7/revoke"): "auth_secret_key_revoke",
        ("POST", "/v1/auth/github/pat"): "auth_github_pat",
        ("GET", "/v1/auth/github/status"): "auth_github_status",
        ("POST", "/v1/auth/github/sync"): "auth_github_sync",
        ("DELETE", "/v1/auth/github"): "auth_github_disconnect",
        ("POST", "/v1/auth/github/device/start"): "auth_github_device_start",
        ("POST", "/v1/auth/github/device/poll"): "auth_github_device_poll",
    }

    for (method, path), bucket in cases.items():
        assert middleware._resolve_bucket(method, path) == bucket
        assert bucket != "auth"

    assert middleware._resolve_bucket("GET", "/v1/auth/unknown") == "auth_other"


@pytest.mark.asyncio
async def test_rate_limit_allows_then_blocks(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(limit=1, window_seconds=1)

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)

    async def call_next(_: Request):
        return Response(status_code=200)

    request1 = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/requests",
            "headers": [],
            "client": ("127.0.0.1", 0),
        }
    )
    first = await middleware.rate_limit_middleware(request1, call_next)
    assert getattr(first, "status_code", None) == 200
    headers1 = getattr(first, "headers", {})
    assert headers1.get("X-RateLimit-Limit") == "1"
    assert headers1.get("X-RateLimit-Remaining") == "0"

    request2 = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/requests",
            "headers": [],
            "client": ("127.0.0.1", 0),
        }
    )
    second = await middleware.rate_limit_middleware(request2, call_next)
    assert getattr(second, "status_code", None) == 429
    headers2 = getattr(second, "headers", None)
    if headers2 is not None:
        assert headers2.get("Retry-After") in {"1", "0"}
    payload = getattr(second, "body", b"") or getattr(second, "content", b"")
    if isinstance(payload, bytes | bytearray):
        data = json.loads(payload)
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {}
    assert data.get("error", {}).get("retry_after") is not None

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_rate_limit_backend_required_returns_503(monkeypatch):
    cfg = DummyCfg(required=True, limit=1, window_seconds=1)
    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return None

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)

    async def call_next(_: Request):
        return Response(status_code=200)

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/requests",
            "headers": [],
            "client": ("127.0.0.1", 0),
        }
    )
    resp = await middleware.rate_limit_middleware(request, call_next)

    assert getattr(resp, "status_code", None) == 503
    body = getattr(resp, "body", b"") or getattr(resp, "content", b"")
    if isinstance(body, bytes | bytearray):
        data = json.loads(body)
    elif isinstance(body, dict):
        data = body
    else:
        data = {}
    assert data.get("error", {}).get("code") == "RATE_LIMIT_BACKEND_UNAVAILABLE"


@pytest.mark.asyncio
async def test_sync_session_stored_in_redis(monkeypatch):
    from unittest.mock import MagicMock

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(limit=1, window_seconds=1)

    # Create a mock session manager since SyncService now requires it
    mock_session_manager = MagicMock()
    svc = SyncService(cfg, mock_session_manager)

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr("app.api.services.sync_service.get_redis", fake_get_redis)

    session = await svc.start_session(user_id=1, client_id="client", limit=50)

    key = redis_key(cfg.redis.prefix, "sync", "session", session.session_id)
    ttl = await redis_client.ttl(key)
    assert ttl > 0
    stored = await redis_client.get(key)
    assert stored is not None

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_rate_limit_uses_webapp_user_id_over_client_host(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(limit=5, window_seconds=60)

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)

    async def call_next(_: Request):
        return Response(status_code=200)

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/requests",
            "headers": [],
            "client": ("127.0.0.1", 0),
        }
    )
    request.state.webapp_user = {"user_id": 555}

    resp = await middleware.rate_limit_middleware(request, call_next)
    assert getattr(resp, "status_code", None) == 200

    keys = await redis_client.keys("*")
    assert any(":rate:requests:555:" in key for key in keys)
    assert not any(":rate:requests:127.0.0.1:" in key for key in keys)

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_rate_limit_buckets_are_isolated(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(limit=1, window_seconds=60)

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)

    async def call_next(_: Request):
        return Response(status_code=200)

    headers = _auth_header(101, "cli-bucket-test")
    request_one = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/requests",
            "headers": headers,
            "client": ("127.0.0.1", 0),
        }
    )
    request_two = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/search",
            "headers": headers,
            "client": ("127.0.0.1", 0),
        }
    )

    first = await middleware.rate_limit_middleware(request_one, call_next)
    second = await middleware.rate_limit_middleware(request_two, call_next)

    assert getattr(first, "status_code", None) == 200
    assert getattr(second, "status_code", None) == 200

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_aggregation_create_rate_limit_blocks_same_user(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(
        limit=100,
        window_seconds=60,
        aggregation_user_limit=1,
        aggregation_client_limit=10,
    )

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)

    async def call_next(_: Request):
        return Response(status_code=200)

    headers = _auth_header(202, "cli-agg-user")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/aggregations",
        "headers": headers,
        "client": ("127.0.0.1", 0),
    }

    first = await middleware.rate_limit_middleware(Request(scope), call_next)
    second = await middleware.rate_limit_middleware(Request(scope), call_next)

    assert getattr(first, "status_code", None) == 200
    assert getattr(second, "status_code", None) == 429
    assert second.headers.get("X-RateLimit-Limit") == "1"

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_aggregation_create_client_limit_blocks_shared_client(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(
        limit=100,
        window_seconds=60,
        aggregation_user_limit=10,
        aggregation_client_limit=1,
    )

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)

    async def call_next(_: Request):
        return Response(status_code=200)

    first = await middleware.rate_limit_middleware(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/aggregations",
                "headers": _auth_header(301, "cli-shared"),
                "client": ("127.0.0.1", 0),
            }
        ),
        call_next,
    )
    second = await middleware.rate_limit_middleware(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/aggregations",
                "headers": _auth_header(302, "cli-shared"),
                "client": ("127.0.0.1", 0),
            }
        ),
        call_next,
    )

    assert getattr(first, "status_code", None) == 200
    assert getattr(second, "status_code", None) == 429
    assert second.headers.get("X-RateLimit-Limit") == "1"

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_secret_login_rate_limit_uses_dedicated_bucket(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(limit=100, window_seconds=60, secret_login_limit=1)

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)
    # client_id only refines the auth bucket when it is in the configured
    # allowlist (MEDIUM-009); otherwise the limiter keys on IP alone.
    monkeypatch.setattr(
        "app.config.settings.ConfigHelper.get_allowed_client_ids",
        lambda: ("secret-client",),
    )

    async def call_next(_: Request):
        return Response(status_code=200)

    body = {
        "user_id": 123456789,
        "client_id": "secret-client",
        "secret": "wrong-secret",
    }

    first = await middleware.rate_limit_middleware(
        _json_request(method="POST", path="/v1/auth/secret-login", body=body),
        call_next,
    )
    second = await middleware.rate_limit_middleware(
        _json_request(method="POST", path="/v1/auth/secret-login", body=body),
        call_next,
    )

    assert getattr(first, "status_code", None) == 200
    assert getattr(second, "status_code", None) == 429
    assert second.headers.get("X-RateLimit-Limit") == "1"

    keys = await redis_client.keys("*")
    assert any(":rate:secret_login:client_id=secret-client|ip=127.0.0.1:" in key for key in keys)

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_credentials_login_rate_limit_uses_client_id_and_ip(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(limit=100, window_seconds=60, credentials_login_limit=5)

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)
    # Both client_ids are allowlisted, so each gets its own bucket (MEDIUM-009).
    monkeypatch.setattr(
        "app.config.settings.ConfigHelper.get_allowed_client_ids",
        lambda: ("ios-app", "android-app"),
    )

    async def call_next(_: Request):
        return Response(status_code=401)

    body = {
        "identifier": "owner@example.com",
        "password": "wrong-password",
        "client_id": "ios-app",
    }
    responses = [
        await middleware.rate_limit_middleware(
            _json_request(method="POST", path="/v1/auth/credentials-login", body=body),
            call_next,
        )
        for _ in range(6)
    ]

    assert [getattr(resp, "status_code", None) for resp in responses[:5]] == [401] * 5
    assert getattr(responses[5], "status_code", None) == 429
    assert responses[5].headers.get("X-RateLimit-Limit") == "5"

    switched = await middleware.rate_limit_middleware(
        _json_request(
            method="POST",
            path="/v1/auth/credentials-login",
            body={**body, "client_id": "android-app"},
        ),
        call_next,
    )
    assert getattr(switched, "status_code", None) == 401

    keys = await redis_client.keys("*")
    assert any(":rate:credentials_login:client_id=ios-app|ip=127.0.0.1:" in key for key in keys)
    assert any(":rate:credentials_login:client_id=android-app|ip=127.0.0.1:" in key for key in keys)
    assert not any(":rate:auth:" in key for key in keys)

    await redis_client.flushall()


@pytest.mark.asyncio
async def test_refresh_rate_limit_uses_refresh_token_client_id(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cfg = DummyCfg(limit=1, window_seconds=60)

    monkeypatch.setattr(middleware, "_cfg_holder", [cfg])
    middleware._redis_warning_logged = False

    async def fake_get_redis(_: DummyCfg):
        return redis_client

    monkeypatch.setattr(middleware, "get_redis", fake_get_redis)
    # web-client is allowlisted, so its refresh token gets a dedicated bucket
    # keyed by client_id + IP (MEDIUM-009).
    monkeypatch.setattr(
        "app.config.settings.ConfigHelper.get_allowed_client_ids",
        lambda: ("web-client",),
    )

    async def call_next(_: Request):
        return Response(status_code=401)

    refresh_token = create_token(123456789, "refresh", client_id="web-client")
    request_body = {"refresh_token": refresh_token}

    first = await middleware.rate_limit_middleware(
        _json_request(method="POST", path="/v1/auth/refresh", body=request_body),
        call_next,
    )
    second = await middleware.rate_limit_middleware(
        _json_request(method="POST", path="/v1/auth/refresh", body=request_body),
        call_next,
    )

    assert getattr(first, "status_code", None) == 401
    assert getattr(second, "status_code", None) == 429

    keys = await redis_client.keys("*")
    assert any(":rate:auth_refresh:client_id=web-client|ip=127.0.0.1:" in key for key in keys)

    await redis_client.flushall()
