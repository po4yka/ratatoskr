"""Tests for GitHub PAT auth endpoints and ManageGitHubIntegrationUseCase.

Covers US-018 (POST /pat), US-020 (GET /status), US-021 (DELETE + use case).
Requires TEST_DATABASE_URL (skipped otherwise).
"""

import logging
from typing import Any

import pytest
import pytest_asyncio
import respx
from cryptography.fernet import Fernet
from httpx import Response
from sqlalchemy import func, select

from app.adapters.github.github_api_client import GitHubAPIClient
from app.api.routers.auth.tokens import create_access_token
from app.application.ports.github_integration import GitHubAuthMethod
from app.application.use_cases.manage_github_integration import (
    ManageGitHubIntegrationUseCase,
)
from app.db.models.repository import Repository, UserGitHubIntegration
from app.db.session import Database
from app.infrastructure.persistence.repositories.github_integration_repository import (
    GitHubIntegrationRepository,
)
from app.security.token_crypto import decrypt_token, reset_key_cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_ID = 555_000_001
_FERNET_KEY = Fernet.generate_key().decode()

# Minimal GitHub /user response payload
_GH_USER_PAYLOAD = {
    "id": 99001,
    "login": "gh-test-user",
    "name": "Test User",
    "email": None,
    "type": "User",
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[misc]
    """Set required env vars and reset caches for every test in this module."""
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_USER_ID))
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", _FERNET_KEY)
    reset_key_cache()
    yield
    reset_key_cache()


@pytest_asyncio.fixture
async def gh_user(db: Any, user_factory: Any):
    """Create the test user in Postgres."""
    return await user_factory(telegram_user_id=_USER_ID, username="gh_test_user")


def _auth_headers() -> dict[str, str]:
    """JWT Bearer headers for _USER_ID."""
    token = create_access_token(_USER_ID, client_id="test")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. POST /pat with valid token stores encrypted token
# ---------------------------------------------------------------------------


async def test_post_pat_with_valid_token_stores_encrypted(
    client: Any, db: Database, gh_user: Any
) -> None:
    """Valid token → 200, encrypted_token bytea is NOT the raw token bytes."""
    raw_token = "ghp_test_valid_token_1234567890"

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(
                200,
                json=_GH_USER_PAYLOAD,
                headers={"X-GitHub-OAuthScopes": "repo, read:user"},
            )
        )
        resp = client.post(
            "/v1/auth/github/pat",
            json={"token": raw_token},
            headers=_auth_headers(),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["login"] == "gh-test-user"
    assert body["github_user_id"] == 99001
    assert body["auth_method"] == "pat"
    assert body["status"] == "active"
    assert raw_token not in resp.text
    assert "encrypted_token" not in body
    assert "token_scopes" not in body

    # Verify DB: encrypted_token is not the raw bytes
    async with db.session() as session:
        row = await session.scalar(
            select(UserGitHubIntegration).where(UserGitHubIntegration.user_id == _USER_ID)
        )
    assert row is not None
    assert row.encrypted_token != raw_token.encode()
    assert decrypt_token(row.encrypted_token) == raw_token


# ---------------------------------------------------------------------------
# 2. POST /pat with invalid token returns 400
# ---------------------------------------------------------------------------


async def test_post_pat_with_invalid_token_returns_400(
    client: Any, db: Database, gh_user: Any
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(401, json={"message": "Bad credentials"})
        )
        resp = client.post(
            "/v1/auth/github/pat",
            json={"token": "ghp_invalid_bad_token_xyz"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "github_token_invalid"
    assert "Invalid or revoked" in body["error"]["message"]
    assert body["error"]["correlation_id"]


# ---------------------------------------------------------------------------
# 3. POST /pat without JWT → 401/403
# ---------------------------------------------------------------------------


async def test_post_pat_requires_jwt(client: Any, db: Database) -> None:
    resp = client.post(
        "/v1/auth/github/pat",
        json={"token": "ghp_some_token_here"},
    )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 4. GET /status with no integration → is_connected=False, repo_count=0
# ---------------------------------------------------------------------------


async def test_get_status_no_integration(client: Any, db: Database, gh_user: Any) -> None:
    resp = client.get("/v1/auth/github/status", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_connected"] is False
    assert body["repo_count"] == 0
    assert body["github_login"] is None


# ---------------------------------------------------------------------------
# 5. GET /status with integration and repos → is_connected=True, repo_count=3
# ---------------------------------------------------------------------------


async def test_get_status_with_integration(
    client: Any, db: Database, gh_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet as _Fernet

    enc = _Fernet(_FERNET_KEY.encode()).encrypt(b"ghp_sometoken")

    async with db.transaction() as session:
        integration = UserGitHubIntegration(
            user_id=_USER_ID,
            auth_method=GitHubAuthMethod.PAT,
            encrypted_token=enc,
            github_login="gh-test-user",
            github_user_id=99001,
        )
        session.add(integration)
        await session.flush()

        # Add 3 repositories for the user
        for i in range(3):
            repo = Repository(
                github_id=10000 + i,
                owner="gh-test-user",
                name=f"repo-{i}",
                full_name=f"gh-test-user/repo-{i}",
                url=f"https://github.com/gh-test-user/repo-{i}",
                user_id=_USER_ID,
            )
            session.add(repo)

    resp = client.get("/v1/auth/github/status", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_connected"] is True
    assert body["github_login"] == "gh-test-user"
    assert body["repo_count"] == 3


# ---------------------------------------------------------------------------
# 6. DELETE /  → 204, row gone
# ---------------------------------------------------------------------------


async def test_delete_revokes_integration(client: Any, db: Database, gh_user: Any) -> None:
    from cryptography.fernet import Fernet as _Fernet

    enc = _Fernet(_FERNET_KEY.encode()).encrypt(b"ghp_sometoken")

    async with db.transaction() as session:
        session.add(
            UserGitHubIntegration(
                user_id=_USER_ID,
                auth_method=GitHubAuthMethod.PAT,
                encrypted_token=enc,
                github_login="gh-test-user",
                github_user_id=99001,
            )
        )

    resp = client.delete("/v1/auth/github", headers=_auth_headers())
    assert resp.status_code == 204

    async with db.session() as session:
        row = await session.scalar(
            select(UserGitHubIntegration).where(UserGitHubIntegration.user_id == _USER_ID)
        )
    assert row is None


# ---------------------------------------------------------------------------
# 7. validate_and_store idempotency — two calls → one row, values updated
# ---------------------------------------------------------------------------


async def test_use_case_validate_and_store_idempotent(db: Database, gh_user: Any) -> None:
    use_case = ManageGitHubIntegrationUseCase(
        repository=GitHubIntegrationRepository(db),
        gateway_factory=GitHubAPIClient,
    )
    token = "ghp_idempotent_token_abc123"

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(
                200,
                json=_GH_USER_PAYLOAD,
                headers={"X-GitHub-OAuthScopes": "repo, read:user"},
            )
        )
        row1, _ = await use_case.validate_and_store(
            token, GitHubAuthMethod.PAT, _USER_ID, correlation_id="cid-1"
        )
        row2, _ = await use_case.validate_and_store(
            token, GitHubAuthMethod.PAT, _USER_ID, correlation_id="cid-2"
        )

    async with db.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(UserGitHubIntegration)
            .where(UserGitHubIntegration.user_id == _USER_ID)
        )

    assert count == 1
    assert row1.github_login == row2.github_login == "gh-test-user"


# ---------------------------------------------------------------------------
# 8. Token must NOT appear in log output
# ---------------------------------------------------------------------------


async def test_token_not_logged(
    client: Any, db: Database, gh_user: Any, caplog: pytest.LogCaptureFixture
) -> None:
    raw_token = "ghp_SUPERSECRET_token_9876543210"

    with caplog.at_level(logging.DEBUG):
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://api.github.com/user").mock(
                return_value=Response(
                    200,
                    json=_GH_USER_PAYLOAD,
                    headers={"X-GitHub-OAuthScopes": "repo, read:user"},
                )
            )
            client.post(
                "/v1/auth/github/pat",
                json={"token": raw_token},
                headers=_auth_headers(),
            )

    for record in caplog.records:
        assert raw_token not in record.getMessage(), (
            f"Plaintext token found in log record: {record.getMessage()}"
        )
        if record.args:
            assert raw_token not in str(record.args), (
                f"Plaintext token found in log args: {record.args}"
            )


def test_token_validation_error_does_not_echo_secret(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    raw_token = "github_pat_" + ("A" * 240)

    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/v1/auth/github/pat",
            json={"token": raw_token},
            headers=_auth_headers(),
        )

    assert resp.status_code == 422
    assert raw_token not in resp.text
    for record in caplog.records:
        assert raw_token not in record.getMessage()
        assert raw_token not in str(record.__dict__)


# ---------------------------------------------------------------------------
# 9. token_scopes column populated after PAT submit
# ---------------------------------------------------------------------------


async def test_pat_stores_token_scopes(client: Any, db: Database, gh_user: Any) -> None:
    """Successful PAT → UserGitHubIntegration.token_scopes populated."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(
                200,
                json=_GH_USER_PAYLOAD,
                headers={"X-GitHub-OAuthScopes": "repo, read:user"},
            )
        )
        resp = client.post(
            "/v1/auth/github/pat",
            json={"token": "ghp_scope_test_token_abcdef"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 200

    async with db.session() as session:
        row = await session.scalar(
            select(UserGitHubIntegration).where(UserGitHubIntegration.user_id == _USER_ID)
        )
    assert row is not None
    assert row.token_scopes is not None
    assert "repo" in row.token_scopes


# ---------------------------------------------------------------------------
# 10. scope_warnings returned in response for overbroad token
# ---------------------------------------------------------------------------


async def test_pat_scope_warnings_in_response(client: Any, db: Database, gh_user: Any) -> None:
    """Overbroad token → 200 with scope_warnings list."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(
                200,
                json=_GH_USER_PAYLOAD,
                headers={"X-GitHub-OAuthScopes": "repo, read:user, delete_repo"},
            )
        )
        resp = client.post(
            "/v1/auth/github/pat",
            json={"token": "ghp_overbroad_token_abcdef"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "scope_warnings" in body
    assert isinstance(body["scope_warnings"], list)
    assert len(body["scope_warnings"]) == 1
    assert "delete repositories" in body["scope_warnings"][0]


# ---------------------------------------------------------------------------
# 11. Insufficient scope → 422
# ---------------------------------------------------------------------------


async def test_pat_insufficient_scope_returns_422(client: Any, db: Database, gh_user: Any) -> None:
    """Token with public_repo but not repo → 422 Unprocessable Entity."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(
                200,
                json=_GH_USER_PAYLOAD,
                headers={"X-GitHub-OAuthScopes": "read:user, public_repo"},
            )
        )
        resp = client.post(
            "/v1/auth/github/pat",
            json={"token": "ghp_narrow_token_abcdef"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "github_token_invalid"
    assert "repo" in body["error"]["message"]
    assert body["error"]["correlation_id"]
