from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.models.responses.common import API_CONTRACT_VERSION, MIN_SUPPORTED_CLIENT_API_VERSION


def test_meta_endpoint_returns_public_compatibility_contract():
    response = TestClient(app).get("/v1/meta")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["apiVersion"] == API_CONTRACT_VERSION
    assert data["appVersion"]
    assert data["minSupportedClientApiVersion"] == MIN_SUPPORTED_CLIENT_API_VERSION
    assert "sync.v1" in data["capabilities"]
    assert isinstance(data["featureFlags"], dict)
    assert isinstance(data["deprecatedRoutes"], list)
    assert body["meta"]["api_version"] == API_CONTRACT_VERSION
