from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.error_handlers import http_exception_handler


def test_main_app_registers_project_http_exception_handler() -> None:
    from app.api.main import app

    assert app.exception_handlers[StarletteHTTPException] is http_exception_handler


@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_type"),
    [
        (401, "UNAUTHORIZED", "authentication"),
        (403, "FORBIDDEN", "authorization"),
        (404, "NOT_FOUND", "not_found"),
    ],
)
def test_router_http_exceptions_use_business_error_envelope(
    status_code: int,
    expected_code: str,
    expected_type: str,
) -> None:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)

    @app.get("/v1/resource")
    async def fail() -> Any:
        headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
        raise HTTPException(
            status_code=status_code,
            detail=f"Failure {status_code}",
            headers=headers,
        )

    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        request.state.correlation_id = "cid-http-exception"
        return await call_next(request)

    response = TestClient(app).get("/v1/resource")
    payload = response.json()

    assert response.status_code == status_code
    assert payload["success"] is False
    assert payload["error"]["code"] == expected_code
    assert payload["error"]["errorType"] == expected_type
    assert payload["error"]["message"] == f"Failure {status_code}"
    assert payload["error"]["correlation_id"] == "cid-http-exception"
    assert payload["meta"]["correlation_id"] == "cid-http-exception"
    assert response.headers["X-Correlation-ID"] == "cid-http-exception"
    if status_code == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "path",
    ["/health/ready", "/static/missing.css", "/missing-spa-route", "/v10/resource"],
)
def test_non_business_http_exceptions_keep_default_semantics(path: str) -> None:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)

    @app.get("/{path:path}")
    async def fail(path: str) -> Any:
        raise HTTPException(
            status_code=503,
            detail=f"Unavailable: {path}",
            headers={"Retry-After": "30"},
        )

    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        request.state.correlation_id = "cid-non-api"
        return await call_next(request)

    response = TestClient(app).get(path)

    assert response.status_code == 503
    assert response.json() == {"detail": f"Unavailable: {path.lstrip('/')}"}
    assert response.headers["Retry-After"] == "30"
    assert response.headers["X-Correlation-ID"] == "cid-non-api"


async def test_business_server_http_exception_hides_internal_detail() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/resource",
            "headers": [],
            "state": {"correlation_id": "cid-server-http-error"},
        }
    )
    secret = "postgresql://user:password@private-host/database"

    response = await http_exception_handler(
        request,
        HTTPException(status_code=503, detail=secret, headers={"Retry-After": "30"}),
    )
    body = response.body.decode("utf-8")
    payload = json.loads(body)

    assert response.status_code == 503
    assert secret not in body
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "An internal server error occurred"
    assert response.headers["Retry-After"] == "30"
    assert response.headers["X-Correlation-ID"] == "cid-server-http-error"
