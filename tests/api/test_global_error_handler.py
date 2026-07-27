from __future__ import annotations

import json
import logging

from starlette.requests import Request

from app.api.error_handlers import global_exception_handler


async def test_global_handler_is_fail_safe_and_does_not_leak_exception_text(
    caplog,
    monkeypatch,
) -> None:
    def failing_load_config(*_args, **_kwargs):
        raise RuntimeError("configuration must not be loaded by the error handler")

    monkeypatch.setattr("app.config.load_config", failing_load_config)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/summaries",
            "headers": [],
            "state": {"correlation_id": "cid-unhandled-error"},
        }
    )
    secret = "postgresql://user:password@private-host/database"

    with caplog.at_level(logging.ERROR):
        response = await global_exception_handler(request, RuntimeError(secret))

    body = bytes(response.body).decode("utf-8")
    payload = json.loads(body)

    assert response.status_code == 500
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "An internal server error occurred"
    assert payload["error"]["details"] is None
    assert payload["error"]["correlation_id"] == "cid-unhandled-error"
    assert secret not in body

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage() == "Unhandled exception"
    assert record.correlation_id == "cid-unhandled-error"
    assert record.exception_type == "RuntimeError"
    assert record.path == "/v1/summaries"
    assert record.exc_info is None
    assert secret not in str(record.__dict__)
