"""Hermetic tests for POST /v1/ai-backups/{service}/session (Mode A ingest).

No Postgres, no Fernet key, no network required.
AiBackupSessionStore.save and AiBackupRepository.mark_authorization_unverified are patched
at the class level so the full route logic is exercised without any real DB
or crypto dependency.

Pattern: minimal FastAPI app + dependency_overrides for get_current_user (mirrors
tests/api/test_backups_trust_api.py), with unittest.mock.patch for the helpers
that are instantiated inside the route body rather than injected via Depends.
"""

from __future__ import annotations

import importlib
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.models.ai_backup import (
    AiAccountBackup,
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)

# Load the router module directly to avoid triggering app.api.routers.__init__,
# which pulls in heavy adapter/di imports (same rationale as test_git_mirrors_router.py).
_ai_backups = importlib.import_module("app.api.routers.ai_backups")

_USER_ID = 42
_SERVICE = "chatgpt"
_URL = f"/v1/ai-backups/{_SERVICE}/session"
_VALID_STATE = {
    "cookies": [
        {
            "name": "__Secure-next-auth.session-token",
            "domain": ".chatgpt.com",
            "value": "session-secret",
            "expires": 4_102_444_800,
        }
    ]
}
_VALID_BODY = {"storage_state": _VALID_STATE}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(*, authenticated: bool = True) -> TestClient:
    """Build a minimal FastAPI app with only the ai_backups router.

    When authenticated=True the get_current_user dependency is replaced with a
    stub that returns a fake user dict (no JWT/DB needed).  When False the stub
    raises 401, simulating an unauthenticated caller.
    """
    app = FastAPI()
    app.include_router(_ai_backups.router)

    if authenticated:
        app.dependency_overrides[_ai_backups.get_ai_backup_owner] = lambda: {"user_id": _USER_ID}
    else:

        def _raise_401() -> None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        app.dependency_overrides[_ai_backups.get_ai_backup_owner] = _raise_401

    return TestClient(app, raise_server_exceptions=False)


def _patched_internals(
    mock_store: MagicMock,
    mock_repo: MagicMock,
):
    """Return a 3-way patch stack covering all DB-touching internals.

    The route body:
      1. calls _get_db(request)  → patched to return a no-op MagicMock DB
      2. does AiBackupSessionStore(db).save(...)  → patched at the class level
         in the session_store module (the import is deferred inside the function
         body, so the module-level patch is the correct target)
      3. calls _get_repo(request).mark_authorization_unverified(...)  → _get_repo itself
         patched to return mock_repo directly
    """
    return (
        patch("app.api.routers.ai_backups._get_db", return_value=MagicMock()),
        patch(
            "app.adapters.ai_backup.session_store.AiBackupSessionStore",
            return_value=mock_store,
        ),
        patch(
            "app.api.routers.ai_backups._get_repo",
            return_value=mock_repo,
        ),
    )


def _mock_store_and_repo() -> tuple[MagicMock, MagicMock]:
    mock_store = MagicMock()
    mock_store.save = AsyncMock()
    mock_store.delete = AsyncMock()
    mock_repo = MagicMock()
    mock_repo.mark_authorization_unverified = AsyncMock()
    mock_repo.mark_authorization_missing = AsyncMock()
    return mock_store, mock_repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_session_returns_204_and_marks_authorization_unverified() -> None:
    """A service-bound session cookie is saved and the route returns 204."""
    mock_store, mock_repo = _mock_store_and_repo()
    p1, p2, p3 = _patched_internals(mock_store, mock_repo)

    with p1, p2, p3:
        resp = _make_client().post(_URL, json=_VALID_BODY)

    assert resp.status_code == 204

    # AiBackupSessionStore.save called with (user_id, service_enum, storage_state_dict)
    mock_store.save.assert_awaited_once()
    save_args = mock_store.save.call_args.args
    assert save_args[0] == _USER_ID
    assert save_args[2] == _VALID_STATE

    mock_repo.mark_authorization_unverified.assert_awaited_once_with(
        _USER_ID, AiBackupService.CHATGPT
    )


def test_first_session_ingest_makes_pending_unverified_status_immediately_readable() -> None:
    class _StatusRepo:
        def __init__(self) -> None:
            self.row: AiAccountBackup | None = None

        async def mark_authorization_unverified(
            self, user_id: int, service: AiBackupService
        ) -> None:
            self.row = AiAccountBackup(
                user_id=user_id,
                service=service,
                status=AiBackupStatus.PENDING,
                authorization_status=AiBackupAuthorizationStatus.UNVERIFIED,
                consecutive_failures=0,
            )

        async def get(self, user_id: int, service: AiBackupService) -> AiAccountBackup | None:
            if self.row is None or self.row.user_id != user_id or self.row.service != service:
                return None
            return self.row

    repo = _StatusRepo()
    mock_store = MagicMock()
    mock_store.save = AsyncMock()
    app = FastAPI()
    app.include_router(_ai_backups.router)
    app.dependency_overrides[_ai_backups.get_ai_backup_owner] = lambda: {"user_id": _USER_ID}
    app.dependency_overrides[_ai_backups.get_current_user] = lambda: {"user_id": _USER_ID}
    app.dependency_overrides[_ai_backups._get_repo] = lambda: repo
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch("app.api.routers.ai_backups._get_db", return_value=MagicMock()),
        patch(
            "app.adapters.ai_backup.session_store.AiBackupSessionStore",
            return_value=mock_store,
        ),
        patch("app.api.routers.ai_backups._get_repo", return_value=repo),
    ):
        ingest_response = client.post(_URL, json=_VALID_BODY)
        status_response = client.get(f"/v1/ai-backups/{_SERVICE}")

    assert ingest_response.status_code == 204
    assert ingest_response.content == b""
    assert b"session-secret" not in ingest_response.content
    assert status_response.status_code == 200
    assert status_response.json()["backup_status"] == "pending"
    assert status_response.json()["authorization_status"] == "unverified"


def test_bad_shape_missing_cookies_key_returns_400() -> None:
    """storage_state dict without a 'cookies' list → 400 before any DB call."""
    resp = _make_client().post(_URL, json={"storage_state": {"no_cookies": True}})

    assert resp.status_code == 400
    assert "cookies" in resp.json()["detail"]


def test_missing_service_session_cookie_returns_400_before_save() -> None:
    mock_store, mock_repo = _mock_store_and_repo()
    p1, p2, p3 = _patched_internals(mock_store, mock_repo)
    body = {
        "storage_state": {
            "cookies": [
                {
                    "name": "cf_clearance",
                    "domain": ".chatgpt.com",
                    "value": "clearance-only",
                    "expires": 4_102_444_800,
                }
            ]
        }
    }

    with p1, p2, p3:
        resp = _make_client().post(_URL, json=body)

    assert resp.status_code == 400
    assert "no usable chatgpt session cookie" in resp.json()["detail"]
    mock_store.save.assert_not_awaited()


def test_non_dict_storage_state_returns_422() -> None:
    """Non-dict storage_state → Pydantic rejects it as 422 (field type mismatch)."""
    resp = _make_client().post(_URL, json={"storage_state": "plainstring"})

    assert resp.status_code == 422


def test_unauthenticated_returns_401() -> None:
    """Caller with no valid JWT → 401 from the get_current_user dependency."""
    resp = _make_client(authenticated=False).post(_URL, json=_VALID_BODY)

    assert resp.status_code == 401


def test_owner_dependency_rejects_authenticated_non_owner() -> None:
    cfg = MagicMock()
    cfg.telegram.allowed_user_ids = (100, 200)

    with patch("app.api.routers.ai_backups._get_app_config", return_value=cfg):
        with pytest.raises(HTTPException) as exc_info:
            _ai_backups.get_ai_backup_owner(MagicMock(), {"user_id": 200})

    assert exc_info.value.status_code == 403


def test_owner_dependency_accepts_first_configured_owner() -> None:
    cfg = MagicMock()
    cfg.telegram.allowed_user_ids = (100, 200)
    user = {"user_id": 100}

    with patch("app.api.routers.ai_backups._get_app_config", return_value=cfg):
        assert _ai_backups.get_ai_backup_owner(MagicMock(), user) is user


def test_response_exposes_backup_and_authorization_independently() -> None:
    checked_at = dt.datetime(2026, 7, 17, tzinfo=dt.UTC)
    row = AiAccountBackup(
        user_id=_USER_ID,
        service=AiBackupService.CHATGPT,
        status=AiBackupStatus.OK,
        authorization_status=AiBackupAuthorizationStatus.EXPIRED,
        authorization_checked_at=checked_at,
        consecutive_failures=0,
    )

    item = _ai_backups._to_item(row)

    assert item.status == "auth_expired"  # backward-compatible combined field
    assert item.backup_status == "ok"
    assert item.authorization_status == "expired"
    assert item.authorization_checked_at == checked_at


def test_storage_state_never_echoed_in_response() -> None:
    """204 carries an empty body; the storage_state value is never returned."""
    mock_store, mock_repo = _mock_store_and_repo()
    p1, p2, p3 = _patched_internals(mock_store, mock_repo)
    sensitive = {
        "cookies": [
            {
                "name": "__Secure-next-auth.session-token",
                "domain": ".chatgpt.com",
                "value": "SUPER_SECRET_TOKEN",
                "expires": 4_102_444_800,
            }
        ]
    }

    with p1, p2, p3:
        resp = _make_client().post(_URL, json={"storage_state": sensitive})

    assert resp.status_code == 204
    assert resp.content == b""
    assert b"SUPER_SECRET_TOKEN" not in resp.content


def test_owner_can_revoke_session_and_mark_authorization_missing() -> None:
    mock_store, mock_repo = _mock_store_and_repo()
    p1, p2, p3 = _patched_internals(mock_store, mock_repo)

    with p1, p2, p3:
        resp = _make_client().delete(_URL)

    assert resp.status_code == 204
    assert resp.content == b""
    mock_store.delete.assert_awaited_once_with(_USER_ID, AiBackupService.CHATGPT)
    mock_repo.mark_authorization_missing.assert_awaited_once_with(_USER_ID, AiBackupService.CHATGPT)


def test_unauthenticated_caller_cannot_revoke_session() -> None:
    resp = _make_client(authenticated=False).delete(_URL)

    assert resp.status_code == 401


def test_authenticated_non_owner_cannot_revoke_session() -> None:
    app = FastAPI()
    app.include_router(_ai_backups.router)
    app.dependency_overrides[_ai_backups.get_current_user] = lambda: {"user_id": 200}
    cfg = MagicMock()
    cfg.telegram.allowed_user_ids = (100,)

    with patch("app.api.routers.ai_backups._get_app_config", return_value=cfg):
        resp = TestClient(app, raise_server_exceptions=False).delete(_URL)

    assert resp.status_code == 403
