from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.responses import Response

from app.api.models.auth import AppleSignInCallbackRequest, MagicLinkRequest
from app.core.time_utils import UTC


class FakeIdentityRepo:
    def __init__(self, _db: Any) -> None:
        self.identity: dict[str, Any] | None = None
        self.user_id_by_email: int | None = 42
        self.consumed: dict[str, Any] | None = {
            "user_id": 42,
            "email": "owner@example.com",
            "email_canonical": "owner@example.com",
            "client_id": "ios-app",
        }
        self.issued: Any = None
        self.upserts: list[dict[str, Any]] = []

    async def async_get_identity(self, *, provider: str, subject: str) -> dict[str, Any] | None:
        return self.identity

    async def async_find_user_id_by_email(self, email_canonical: str) -> int | None:
        return self.user_id_by_email

    async def async_upsert_identity(self, **kwargs: Any) -> dict[str, Any]:
        self.upserts.append(kwargs)
        return kwargs

    async def async_issue_magic_link(self, **kwargs: Any) -> Any:
        self.issued = SimpleNamespace(
            user_id=kwargs["user_id"],
            email=kwargs["email"],
            token="magic-token-value",
            expires_at=datetime(2026, 6, 19, tzinfo=UTC),
        )
        return self.issued

    async def async_consume_magic_link(self, token: str) -> dict[str, Any] | None:
        return self.consumed if token == "magic-token-value" else None


class FakeEmailService:
    def __init__(self, _cfg: Any) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_magic_link(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"email_sent": True, "delivery_id": "delivery-1"}


@pytest.mark.asyncio
async def test_magic_link_request_sends_email(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routers.auth import magic_link

    repo = FakeIdentityRepo(None)
    email = FakeEmailService(None)
    monkeypatch.setattr(magic_link, "validate_client_id", lambda _client_id: None)
    monkeypatch.setattr(magic_link, "ensure_user_allowed", lambda _user_id: None)
    monkeypatch.setattr(magic_link, "get_session_manager", lambda: object())
    monkeypatch.setattr(magic_link, "UserIdentityRepository", lambda _db: repo)
    monkeypatch.setattr(magic_link, "EmailDeliveryService", lambda _cfg: email)
    monkeypatch.setattr(
        magic_link,
        "load_config",
        lambda allow_stub_telegram=True: SimpleNamespace(
            auth=SimpleNamespace(magic_link_verify_url="https://app.example/magic"),
            email=SimpleNamespace(),
        ),
    )

    response = await magic_link.request_magic_link(
        MagicLinkRequest(email="Owner@Example.com", client_id="ios-app")
    )

    assert response["data"]["status"] == "sent"
    assert repo.issued.user_id == 42
    assert email.sent[0]["recipient"] == "Owner@Example.com"
    assert email.sent[0]["link"].startswith("https://app.example/magic?token=magic-token-value")


@pytest.mark.asyncio
async def test_magic_link_verify_consumes_token_and_issues_standard_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routers.auth import magic_link

    repo = FakeIdentityRepo(None)
    monkeypatch.setattr(magic_link, "ensure_user_allowed", lambda _user_id: None)
    monkeypatch.setattr(magic_link, "get_session_manager", lambda: object())
    monkeypatch.setattr(magic_link, "UserIdentityRepository", lambda _db: repo)

    async def fake_issue(**kwargs: Any) -> dict[str, Any]:
        return {"data": {"user_id": kwargs["user_id"], "client_id": kwargs["client_id"]}}

    monkeypatch.setattr(magic_link, "issue_auth_tokens", fake_issue)

    response = await magic_link.verify_magic_link(Response(), token="magic-token-value")

    assert response["data"] == {"user_id": 42, "client_id": "ios-app"}
    assert repo.upserts[0]["provider"] == "magic_link"
    assert repo.upserts[0]["subject"] == "owner@example.com"


@pytest.mark.asyncio
async def test_apple_callback_links_by_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routers.auth import apple

    repo = FakeIdentityRepo(None)
    monkeypatch.setattr(apple, "validate_client_id", lambda _client_id: None)
    monkeypatch.setattr(apple, "ensure_user_allowed", lambda _user_id: None)
    monkeypatch.setattr(apple, "get_session_manager", lambda: object())
    monkeypatch.setattr(apple, "UserIdentityRepository", lambda _db: repo)
    monkeypatch.setattr(
        apple,
        "load_config",
        lambda allow_stub_telegram=True: SimpleNamespace(
            auth=SimpleNamespace(apple_client_id="com.example.app")
        ),
    )

    async def _fake_validate(_token, audience, nonce):
        return {
            "sub": "apple-subject",
            "email": "Owner@privaterelay.appleid.com",
            "email_verified": "true",
            "nonce": nonce,
        }

    monkeypatch.setattr(apple, "_validate_apple_id_token", _fake_validate)

    async def fake_issue(**kwargs: Any) -> dict[str, Any]:
        return {"data": {"user_id": kwargs["user_id"], "client_id": kwargs["client_id"]}}

    monkeypatch.setattr(apple, "issue_auth_tokens", fake_issue)

    response = await apple.apple_callback(
        AppleSignInCallbackRequest(
            id_token="id-token",
            client_id="ios-app",
            nonce="nonce",
        ),
        Response(),
    )

    assert response["data"] == {"user_id": 42, "client_id": "ios-app"}
    assert repo.upserts[0]["provider"] == "apple"
    assert repo.upserts[0]["subject"] == "apple-subject"
    assert repo.upserts[0]["email_canonical"] == "owner@privaterelay.appleid.com"


def test_apple_callback_request_requires_nonce() -> None:
    """The callback contract must reject a missing nonce at the edge (422) so the
    replay-protection check can no longer be skipped by omitting it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppleSignInCallbackRequest(id_token="id-token", client_id="ios-app")
    with pytest.raises(ValidationError):
        AppleSignInCallbackRequest(id_token="id-token", client_id="ios-app", nonce="")


@pytest.mark.asyncio
async def test_apple_id_token_nonce_is_always_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """_validate_apple_id_token must bind the id_token to the expected nonce on
    every path -- a matching nonce passes; a mismatched, missing, or empty nonce
    is rejected (previously a None expected nonce silently skipped the check)."""
    from app.api.routers.auth import apple

    monkeypatch.setattr(
        apple._APPLE_JWKS_CLIENT,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key="public-key"),
    )

    token_claims: dict[str, Any] = {}

    def _fake_decode(_token: Any, _key: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(token_claims)

    monkeypatch.setattr(apple.jwt, "decode", _fake_decode)

    # Matching nonce -> accepted.
    token_claims.clear()
    token_claims.update({"sub": "apple-subject", "nonce": "expected-nonce"})
    claims = await apple._validate_apple_id_token(
        "id-token", audience="aud", nonce="expected-nonce"
    )
    assert claims["sub"] == "apple-subject"

    # Mismatched nonce (replayed token from a different auth request) -> rejected.
    token_claims.clear()
    token_claims.update({"sub": "apple-subject", "nonce": "attacker-nonce"})
    with pytest.raises(apple.AuthenticationError):
        await apple._validate_apple_id_token("id-token", audience="aud", nonce="expected-nonce")

    # Token carries no nonce claim at all -> rejected (no silent skip).
    token_claims.clear()
    token_claims.update({"sub": "apple-subject"})
    with pytest.raises(apple.AuthenticationError):
        await apple._validate_apple_id_token("id-token", audience="aud", nonce="expected-nonce")

    # Blank expected nonce -> rejected before any decode (defense in depth).
    with pytest.raises(apple.AuthenticationError):
        await apple._validate_apple_id_token("id-token", audience="aud", nonce="   ")
