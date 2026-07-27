from __future__ import annotations

import json
import logging

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError as PydanticValidationError
from starlette.requests import Request

from app.api.error_handlers import (
    pydantic_validation_exception_handler,
    validation_exception_handler,
)


async def test_validation_handler_does_not_echo_token_input(
    caplog,
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/auth/github/pat",
            "headers": [],
            "state": {"correlation_id": "cid-redaction"},
        }
    )
    raw_token = "github_pat_" + ("A" * 240)
    exc = RequestValidationError(
        [
            {
                "type": "string_too_long",
                "loc": ("body", "token"),
                "msg": "String should have at most 200 characters",
                "input": raw_token,
            }
        ]
    )

    with caplog.at_level(logging.WARNING):
        response = await validation_exception_handler(request, exc)

    body = bytes(response.body).decode("utf-8")
    payload = json.loads(body)

    assert response.status_code == 422
    assert payload["error"]["details"]["fields"][0]["field"] == "body.token"
    assert raw_token not in body
    for record in caplog.records:
        assert raw_token not in record.getMessage()
        assert raw_token not in str(record.__dict__)


async def test_internal_pydantic_validation_error_is_sanitized_server_error(
    caplog,
) -> None:
    class ResponseModel(BaseModel):
        count: int

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/summaries",
            "headers": [],
            "state": {"correlation_id": "cid-response-validation"},
        }
    )
    rejected_value = "secret-response-value"
    try:
        ResponseModel.model_validate({"count": rejected_value})
    except PydanticValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover - protects the regression test setup
        raise AssertionError("Expected Pydantic validation to fail")

    with caplog.at_level(logging.ERROR):
        response = await pydantic_validation_exception_handler(request, validation_error)

    body = bytes(response.body).decode("utf-8")
    payload = json.loads(body)

    assert response.status_code == 500
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "An internal server error occurred"
    assert payload["error"]["details"] is None
    assert rejected_value not in body
    for record in caplog.records:
        assert rejected_value not in record.getMessage()
        assert rejected_value not in str(record.__dict__)


def test_validation_exception_handlers_are_registered_by_failure_origin() -> None:
    from app.api.main import app

    assert app.exception_handlers[PydanticValidationError] is pydantic_validation_exception_handler
    assert app.exception_handlers[RequestValidationError] is validation_exception_handler
