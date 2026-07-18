from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import metrics_auth
from app.config.api import AuthConfig


def _request(token: str | None) -> Request:
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "headers": headers})


@pytest.mark.asyncio
async def test_metrics_bearer_accepts_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "m" * 32
    monkeypatch.setattr(
        metrics_auth,
        "load_config",
        lambda **_kwargs: SimpleNamespace(auth=SimpleNamespace(metrics_bearer_token=token)),
    )

    await metrics_auth.require_metrics_bearer(_request(token))


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, "m" * 32])
async def test_metrics_bearer_rejects_missing_or_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    monkeypatch.setattr(
        metrics_auth,
        "load_config",
        lambda **_kwargs: SimpleNamespace(auth=SimpleNamespace(metrics_bearer_token=configured)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await metrics_auth.require_metrics_bearer(_request("wrong"))

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_metrics_bearer_token_config_requires_high_entropy_length() -> None:
    assert AuthConfig(metrics_bearer_token="m" * 32).metrics_bearer_token == "m" * 32
    with pytest.raises(ValueError, match="32 to 512"):
        AuthConfig(metrics_bearer_token="short")
