from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from fastapi.exceptions import RequestValidationError

from app.api.error_handlers import validation_exception_handler


class _Request:
    state = SimpleNamespace(correlation_id="cid-redaction")
    url = SimpleNamespace(path="/v1/auth/github/pat")


async def test_validation_handler_does_not_echo_token_input(
    caplog,
) -> None:
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
        response = await validation_exception_handler(_Request(), exc)

    body = response.body.decode("utf-8")
    payload = json.loads(body)

    assert response.status_code == 422
    assert payload["error"]["details"]["fields"][0]["field"] == "body.token"
    assert raw_token not in body
    for record in caplog.records:
        assert raw_token not in record.getMessage()
        assert raw_token not in str(record.__dict__)
