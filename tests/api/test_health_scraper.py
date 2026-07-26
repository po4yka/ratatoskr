from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.exceptions import AuthorizationError
from app.api.routers.auth.tokens import create_access_token
from app.api.services.auth_service import AuthService


def _auth_headers() -> dict[str, str]:
    # Use an ID from the default test ALLOWED_USER_IDS set in tests/conftest.py
    token = create_access_token(user_id=123456789, username="health_test_user", client_id="test")
    return {"Authorization": f"Bearer {token}"}


def test_unknown_health_path_does_not_fall_through_to_spa(client: TestClient, monkeypatch) -> None:
    from app.api import main

    def _unexpected_spa_response():
        raise AssertionError("reserved health routes must not be served by the SPA")

    monkeypatch.setattr(main, "_serve_web_index", _unexpected_spa_response)

    response = client.get("/healthz")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_health_detailed_authorizes_before_running_probes(monkeypatch) -> None:
    from app.api.routers import health

    require_owner = AsyncMock(side_effect=AuthorizationError("Owner permissions required"))
    database_probe = AsyncMock()
    monkeypatch.setattr(AuthService, "require_owner", require_owner)
    monkeypatch.setattr(health, "_check_database", database_probe)

    with pytest.raises(AuthorizationError):
        await health.detailed_health_check(
            SimpleNamespace(),
            {"user_id": 123456789, "username": "health_test_user", "client_id": "test"},
        )

    require_owner.assert_awaited_once()
    database_probe.assert_not_awaited()


def test_health_detailed_requires_owner(client: TestClient, monkeypatch) -> None:
    require_owner = AsyncMock(side_effect=AuthorizationError("Owner permissions required"))
    monkeypatch.setattr(AuthService, "require_owner", require_owner)

    response = client.get("/health/detailed", headers=_auth_headers())

    assert response.status_code == 403
    require_owner.assert_awaited_once()


def test_health_detailed_includes_scraper_component(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(AuthService, "require_owner", AsyncMock(return_value={}))

    response = client.get("/health/detailed", headers=_auth_headers())
    assert response.status_code == 200

    payload = response.json()["data"]
    components = payload["components"]

    assert "scraper" in components
    scraper = components["scraper"]
    assert "status" in scraper
    assert "provider_order_effective" in scraper or "error" in scraper


def test_health_detailed_reuses_cached_database_details(client: TestClient, monkeypatch) -> None:
    from app.api.routers import health

    monkeypatch.setattr(AuthService, "require_owner", AsyncMock(return_value={}))

    class _Inspection:
        def __init__(self) -> None:
            self.size_calls = 0
            self.integrity_calls = 0

        async def async_database_size_mb(self) -> float:
            self.size_calls += 1
            return 4.0

        async def async_check_integrity(self) -> tuple[bool, str]:
            self.integrity_calls += 1
            return True, "ok"

    class _Database:
        def __init__(self) -> None:
            self.inspection = _Inspection()
            self.healthcheck_calls = 0

        async def healthcheck(self) -> None:
            self.healthcheck_calls += 1

    database = _Database()
    runtime = SimpleNamespace(
        db=database,
        search=SimpleNamespace(vector_store=None),
    )

    health.clear_health_check_cache()
    monkeypatch.setattr(health, "resolve_api_runtime", lambda _request: runtime)
    monkeypatch.setattr(
        health,
        "_check_redis",
        AsyncMock(return_value={"status": "disabled", "latency_ms": 0}),
    )
    monkeypatch.setattr(
        health,
        "_check_scraper",
        AsyncMock(return_value={"status": "healthy", "latency_ms": 0}),
    )

    headers = _auth_headers()
    response_one = client.get("/health/detailed", headers=headers)
    response_two = client.get("/health/detailed", headers=headers)

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    assert database.healthcheck_calls == 2
    assert database.inspection.size_calls == 1
    assert database.inspection.integrity_calls == 1
