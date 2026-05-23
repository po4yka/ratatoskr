from __future__ import annotations

from types import SimpleNamespace

from app.api.services.diagnostics_service import _social_connection_diagnostics
from app.infrastructure.persistence.repositories.admin_read_repository import (
    _redact_message,
    _safe_social_attempt_metadata,
)


def test_social_connection_diagnostics_exposes_safe_provider_summary() -> None:
    cfg = SimpleNamespace(
        social=SimpleNamespace(
            instagram_client_id=None,
            instagram_client_secret=None,
            instagram_redirect_uri=None,
            threads_client_id="threads-client",
            threads_client_secret="threads-secret",
            threads_redirect_uri="https://example.test/threads/callback",
        ),
        twitter=SimpleNamespace(
            x_oauth_client_id="x-client",
            x_oauth_redirect_uri="https://example.test/x/callback",
        ),
    )
    persisted = [
        {
            "provider": "x",
            "active_connection_count": 2,
            "needs_reauth_count": 1,
            "recent_fetch_failures": [
                {
                    "provider": "x",
                    "attempt_type": "url_extract",
                    "error_code": "RATE_LIMIT",
                    "error_message": _redact_message("token=raw-token"),
                    "occurred_at": None,
                    "metadata": _safe_social_attempt_metadata(
                        {
                            "rate_limit": {
                                "reset": "2026-05-23T12:30:00Z",
                                "access_token": "raw-token",
                            },
                            "source_payload": {"access_token": "raw-token"},
                        }
                    ),
                }
            ],
            "rate_limit_reset_summary": "2026-05-23T12:30:00Z",
        }
    ]

    providers = _social_connection_diagnostics(persisted=persisted, cfg=cfg)
    payload = [item.model_dump(mode="json") for item in providers]
    rendered = str(payload)

    by_provider = {item["provider"]: item for item in payload}
    assert by_provider["instagram"]["configured"] is False
    assert by_provider["threads"]["configured"] is True
    assert by_provider["x"]["configured"] is True
    assert by_provider["x"]["active_connection_count"] == 2
    assert by_provider["x"]["needs_reauth_count"] == 1
    assert by_provider["x"]["recent_fetch_failures"][0]["error_code"] == "RATE_LIMIT"
    assert by_provider["x"]["rate_limit_reset_summary"] == "2026-05-23T12:30:00Z"
    assert "raw-token" not in rendered
    assert "source_payload" not in rendered
