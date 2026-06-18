from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.api.routers.auth.tokens import create_access_token
from app.mcp.http_auth import McpHttpAuthMiddleware, authenticate_mcp_http_headers


def test_authenticate_mcp_http_headers_accepts_direct_bearer(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "mcp-public-v1")
    monkeypatch.setenv("ALLOWED_USER_IDS", "123")

    token = create_access_token(user_id=123, username="mcp-user", client_id="mcp-public-v1")
    result = authenticate_mcp_http_headers(
        {"authorization": f"Bearer {token}"},
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret=None,
    )

    assert result.identity is not None
    assert result.identity.user_id == 123
    assert result.identity.client_id == "mcp-public-v1"
    assert result.identity.username == "mcp-user"
    assert result.identity.auth_source == "authorization"


def test_authenticate_mcp_http_headers_rejects_empty_allowed_user_ids(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "mcp-public-v1")
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)

    token = create_access_token(user_id=123, username="mcp-user", client_id="mcp-public-v1")
    result = authenticate_mcp_http_headers(
        {"authorization": f"Bearer {token}"},
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret=None,
    )

    assert result.identity is None
    assert result.status_code == 403
    assert result.error == "User not authorized"


def test_authenticate_mcp_http_headers_accepts_forwarded_bearer(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "mcp-public-v1")
    monkeypatch.setenv("ALLOWED_USER_IDS", "456")

    token = create_access_token(user_id=456, username="gateway-user", client_id="mcp-public-v1")
    result = authenticate_mcp_http_headers(
        {
            "x-ratatoskr-forwarded-access-token": token,
            "x-ratatoskr-mcp-forwarding-secret": "shared-secret",
        },
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret="shared-secret",
    )

    assert result.identity is not None
    assert result.identity.user_id == 456
    assert result.identity.auth_source == "forwarded_bearer"


def test_authenticate_mcp_http_headers_rejects_bad_forwarding_secret(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "mcp-public-v1")
    monkeypatch.setenv("ALLOWED_USER_IDS", "789")

    token = create_access_token(user_id=789, username="gateway-user", client_id="mcp-public-v1")
    result = authenticate_mcp_http_headers(
        {
            "x-ratatoskr-forwarded-access-token": token,
            "x-ratatoskr-mcp-forwarding-secret": "wrong-secret",
        },
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret="shared-secret",
    )

    assert result.identity is None
    assert result.status_code == 401
    assert result.error == "Forwarded token credentials are invalid"


def test_mcp_http_auth_middleware_populates_request_state(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "mcp-public-v1")
    monkeypatch.setenv("ALLOWED_USER_IDS", "222")

    async def whoami(request):
        identity = request.state.mcp_identity
        return JSONResponse(
            {
                "user_id": identity.user_id,
                "client_id": identity.client_id,
                "auth_source": identity.auth_source,
            }
        )

    token = create_access_token(user_id=222, username="middleware-user", client_id="mcp-public-v1")
    app = Starlette(routes=[Route("/whoami", whoami)])
    app_asgi: Any = McpHttpAuthMiddleware(
        app,
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret=None,
    )

    with TestClient(app_asgi) as client:
        response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "user_id": 222,
        "client_id": "mcp-public-v1",
        "auth_source": "authorization",
    }


def test_mcp_http_auth_middleware_rejects_missing_auth() -> None:
    async def whoami(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/whoami", whoami)])
    app_asgi: Any = McpHttpAuthMiddleware(
        app,
        forwarded_access_token_header="X-Ratatoskr-Forwarded-Access-Token",
        forwarded_secret_header="X-Ratatoskr-MCP-Forwarding-Secret",
        forwarding_secret=None,
    )

    with TestClient(app_asgi) as client:
        response = client.get("/whoami")

    assert response.status_code == 401
    assert response.json() == {
        "error": "mcp_auth_failed",
        "message": "Authentication required",
    }
