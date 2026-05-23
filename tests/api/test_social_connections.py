from __future__ import annotations

from types import SimpleNamespace

from app.api.models.responses.social import SocialConnectionResponse
from app.api.routers.social_auth import _connection_response


def test_connection_response_exposes_safe_status_fields_without_tokens() -> None:
    dto = SimpleNamespace(
        provider="threads",
        connected=True,
        auth_type="oauth2",
        provider_user_id="threads-user-id",
        provider_username="threads_user",
        token_scopes=["threads_basic"],
        access_token_expires_at="2026-06-23T00:00:00Z",
        refresh_token_expires_at="2026-06-23T00:00:00Z",
        last_used_at="2026-05-23T12:00:00Z",
        status="needs_reauth",
        connected_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-23T12:01:00Z",
        metadata_json={
            "threads_account": {"id": "threads-user-id", "username": "threads_user"},
            "access_token": "raw-token",
            "source_payload": {"secret": "raw-source"},
        },
    )

    response = _connection_response(dto)
    payload = response.model_dump(by_alias=True)
    rendered = response.model_dump_json(by_alias=True)

    assert isinstance(response, SocialConnectionResponse)
    assert payload["provider"] == "threads"
    assert payload["status"] == "needs_reauth"
    assert payload["providerUsername"] == "threads_user"
    assert payload["scopes"] == ["threads_basic"]
    assert payload["capabilities"]["provider"] == "threads"
    assert payload["capabilities"]["supportsTimelineIngestion"] is True
    assert payload["capabilities"]["supportedScopes"] == ["threads_basic"]
    assert payload["expiresAt"] == "2026-06-23T00:00:00Z"
    assert payload["lastUsedAt"] == "2026-05-23T12:00:00Z"
    assert payload["createdAt"] == "2026-05-01T00:00:00Z"
    assert payload["updatedAt"] == "2026-05-23T12:01:00Z"
    assert "raw-token" not in rendered
    assert "raw-source" not in rendered
    assert "encrypted" not in rendered
