from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from app.adapters.twitter.api_extractor import XApiPostExtractor
from app.application.ports.social_connections import SocialFetchAttemptCreate
from app.core.logging_utils import redact_for_logging
from app.infrastructure.persistence.repositories.social_connection_repository import (
    SocialConnectionRepositoryAdapter,
    _sanitize_fetch_attempt_metadata,
)
from app.security.secret_crypto import reset_secret_key_cache
from tests.adapters.twitter.test_x_api_extractor import (
    FakeSocialConnectionRepository,
    FakeXClient,
    _connection,
)


@pytest.fixture(autouse=True)
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    reset_secret_key_cache()
    yield
    reset_secret_key_cache()


def test_social_redaction_removes_oauth_secrets_from_dicts_and_text() -> None:
    payload = {
        "access_token": "access-token-secret",
        "refresh_token": "refresh-token-secret",
        "code": "authorization-code-secret",
        "state": "oauth-state-secret",
        "Cookie": "sid=cookie-secret",
        "Authorization": "Bearer authorization-header-secret",
        "callback_url": (
            "https://example.test/callback?"
            "code=authorization-code-secret&state=oauth-state-secret&access_token=access-token-secret"
        ),
        "message": (
            "Authorization: Bearer authorization-header-secret; "
            "Cookie: sid=cookie-secret; "
            "code=authorization-code-secret state=oauth-state-secret"
        ),
    }

    redacted = redact_for_logging(payload)
    rendered = str(redacted)

    assert "access-token-secret" not in rendered
    assert "refresh-token-secret" not in rendered
    assert "authorization-code-secret" not in rendered
    assert "oauth-state-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "authorization-header-secret" not in rendered


def test_social_fetch_attempt_metadata_sanitizer_drops_provider_payload_and_secrets() -> None:
    sanitized = _sanitize_fetch_attempt_metadata(
        {
            "auth_strategy": {
                "selected_tier": "x_api",
                "access_token": "access-token-secret",
                "state": "oauth-state-secret",
            },
            "api_status": "429",
            "connection_id": 10,
            "provider_resource_id": "123",
            "rate_limit": {"reset": "1779519999", "cookie": "sid=cookie-secret"},
            "raw_provider_payload": {"access_token": "access-token-secret"},
            "headers": {"Authorization": "Bearer authorization-header-secret"},
            "code": "authorization-code-secret",
            "state": "oauth-state-secret",
            "cookies": "sid=cookie-secret",
        }
    )
    rendered = str(sanitized)

    assert sanitized == {
        "api_status": "429",
        "auth_strategy": {"selected_tier": "x_api"},
        "connection_id": 10,
        "provider_resource_id": "123",
        "rate_limit": {"reset": "1779519999"},
    }
    assert "access-token-secret" not in rendered
    assert "authorization-code-secret" not in rendered
    assert "oauth-state-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "authorization-header-secret" not in rendered


class _CaptureTransaction:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def __aenter__(self) -> _CaptureTransaction:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def add(self, row: Any) -> None:
        self.rows.append(row)


class _CaptureDb:
    def __init__(self) -> None:
        self.tx = _CaptureTransaction()

    def transaction(self) -> _CaptureTransaction:
        return self.tx


@pytest.mark.asyncio
async def test_social_fetch_attempt_repository_stores_sanitized_metadata_only() -> None:
    db = _CaptureDb()
    repo = SocialConnectionRepositoryAdapter(db)  # type: ignore[arg-type]

    await repo.record_fetch_attempt(
        SocialFetchAttemptCreate(
            user_id=777,
            provider="x",
            attempt_type="post_lookup",
            status="failed",
            error_code="rate_limited",
            error_message="rate_limited",
            metadata_json={
                "auth_strategy": {
                    "selected_tier": "x_api",
                    "access_token": "access-token-secret",
                },
                "api_status": "429",
                "provider_resource_id": "123",
                "raw_provider_payload": {"refresh_token": "refresh-token-secret"},
                "headers": {"Authorization": "Bearer authorization-header-secret"},
                "state": "oauth-state-secret",
                "cookies": "sid=cookie-secret",
            },
        )
    )

    assert len(db.tx.rows) == 1
    metadata = db.tx.rows[0].metadata_json
    rendered = str(metadata)
    assert metadata == {
        "api_status": "429",
        "auth_strategy": {"selected_tier": "x_api"},
        "provider_resource_id": "123",
    }
    assert "access-token-secret" not in rendered
    assert "refresh-token-secret" not in rendered
    assert "authorization-header-secret" not in rendered
    assert "oauth-state-secret" not in rendered
    assert "cookie-secret" not in rendered


@pytest.mark.asyncio
async def test_social_content_fetch_logs_correlation_id_without_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = FakeSocialConnectionRepository(_connection())
    extractor = XApiPostExtractor(
        repository=repo,
        x_client=FakeXClient(httpx.Response(401, json={})),
    )

    with caplog.at_level(logging.INFO, logger="app.adapters.twitter.api_extractor"):
        await extractor.extract(
            url_text="https://x.com/example/status/123",
            user_id=777,
            correlation_id="cid-social-redaction",
            metadata={"tier_outcomes": {}},
        )

    rendered_logs = "\n".join(
        record.getMessage() + str(record.__dict__) for record in caplog.records
    )
    assert "cid-social-redaction" in rendered_logs
    assert "old-access" not in rendered_logs
    assert "Bearer" not in rendered_logs
    assert len(repo.attempts) == 1
    attempt = repo.attempts[0]
    assert attempt.user_id == 777
    assert attempt.provider == "x"
    assert attempt.connection_id == 10
    assert attempt.attempt_type == "post_lookup"
    assert attempt.status == "failed"
    assert attempt.error_code == "unauthorized"
    assert attempt.error_message == "unauthorized"
    assert attempt.source_url == "https://x.com/example/status/123"
    assert attempt.normalized_url == "https://x.com/example/status/123"
    assert attempt.provider_resource_id == "123"
    assert attempt.http_status == 401
    assert attempt.auth_tier == "x_api"
    assert attempt.correlation_id == "cid-social-redaction"
    assert attempt.metadata_json == {
        "auth_strategy": {"selected_tier": "x_api"},
        "api_status": "401",
        "provider_resource_id": "123",
        "source_url": "https://x.com/example/status/123",
        "normalized_url": "https://x.com/example/status/123",
        "connection_id": 10,
        "correlation_id": "cid-social-redaction",
    }
    assert "old-access" not in str(attempt)
    assert "Bearer" not in str(attempt)
