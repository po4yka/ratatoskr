# ruff: noqa: RUF059
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.services.webhook_service import (
    build_webhook_payload,
    generate_webhook_secret,
    sign_payload,
    validate_webhook_url,
    verify_signature,
)


class TestGenerateWebhookSecret:
    def test_returns_64_char_hex_string(self):
        secret = generate_webhook_secret()
        assert len(secret) == 64
        assert all(c in "0123456789abcdef" for c in secret)

    def test_different_on_each_call(self):
        s1 = generate_webhook_secret()
        s2 = generate_webhook_secret()
        assert s1 != s2


class TestSignPayload:
    def test_consistent_for_same_input(self):
        secret = "test-secret"
        payload = b'{"event": "test"}'
        sig1 = sign_payload(secret, payload)
        sig2 = sign_payload(secret, payload)
        assert sig1 == sig2

    def test_different_for_different_payload(self):
        secret = "test-secret"
        sig1 = sign_payload(secret, b"payload-a")
        sig2 = sign_payload(secret, b"payload-b")
        assert sig1 != sig2

    def test_different_for_different_secret(self):
        payload = b"same-payload"
        sig1 = sign_payload("secret-a", payload)
        sig2 = sign_payload("secret-b", payload)
        assert sig1 != sig2


class TestVerifySignature:
    def test_valid_signature(self):
        secret = "my-secret"
        payload = b'{"data": "value"}'
        sig = sign_payload(secret, payload)
        assert verify_signature(secret, payload, sig) is True

    def test_invalid_signature(self):
        secret = "my-secret"
        payload = b'{"data": "value"}'
        assert verify_signature(secret, payload, "bad-signature") is False

    def test_tampered_payload(self):
        secret = "my-secret"
        original = b'{"data": "value"}'
        sig = sign_payload(secret, original)
        tampered = b'{"data": "tampered"}'
        assert verify_signature(secret, tampered, sig) is False


class TestValidateWebhookUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/webhook",
            "http://localhost:8000/hook",
            "https://api.example.com:8443/hook",
            "http://127.0.0.1/hook",
        ],
    )
    def test_valid_urls(self, url):
        with patch(
            "app.domain.services.webhook_service.is_webhook_url_safe",
            return_value=(True, None),
        ):
            valid, error = validate_webhook_url(url)
        assert valid is True
        assert error is None

    def test_rejects_ftp_scheme(self):
        valid, error = validate_webhook_url("ftp://example.com")
        assert valid is False
        assert error is not None

    def test_rejects_private_ip(self):
        valid, error = validate_webhook_url("http://10.0.0.1/hook")
        assert valid is False

    def test_rejects_private_ip_192(self):
        valid, error = validate_webhook_url("http://192.168.1.1/hook")
        assert valid is False

    def test_rejects_empty_string(self):
        valid, error = validate_webhook_url("")
        assert valid is False

    def test_rejects_no_scheme(self):
        valid, error = validate_webhook_url("example.com/webhook")
        assert valid is False

    def test_rejects_http_non_localhost(self):
        valid, error = validate_webhook_url("http://example.com/hook")
        assert valid is False


class TestBuildWebhookPayload:
    def test_has_required_keys(self):
        payload = build_webhook_payload("summary.created", {"id": 1})
        assert "event" in payload
        assert "timestamp" in payload
        assert "data" in payload

    def test_event_type_matches(self):
        payload = build_webhook_payload("summary.created", {"id": 1})
        assert payload["event"] == "summary.created"

    def test_data_matches(self):
        data = {"id": 42, "title": "Test"}
        payload = build_webhook_payload("test.event", data)
        assert payload["data"] == data

    def test_timestamp_is_iso_format(self):
        from datetime import datetime

        payload = build_webhook_payload("test.event", {})
        # Should parse without error
        datetime.fromisoformat(payload["timestamp"])


# ---------------------------------------------------------------------------
# SSRF tests for send_test_webhook (router-level, no DB required)
# ---------------------------------------------------------------------------


def _make_sub(url: str) -> dict:
    """Return a minimal subscription dict for send_test_webhook."""
    return {
        "id": 1,
        "user": 42,
        "url": url,
        "secret": "a" * 64,
        "name": "test",
        "events_json": ["test"],
        "enabled": True,
        "status": "active",
        "failure_count": 0,
        "last_delivery_at": None,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }


def _make_delivery() -> dict:
    """Return a minimal delivery log dict."""
    return {
        "id": 1,
        "event_type": "test",
        "response_status": 200,
        "success": True,
        "attempt": 1,
        "duration_ms": 10,
        "error": None,
        "created_at": "2024-01-01T00:00:00+00:00",
    }


class TestSendTestWebhookSSRF:
    """Verify that send_test_webhook blocks private/reserved IPs and allows public URLs.

    Tests call the route coroutine directly (no live DB or HTTP server required).
    The SSRF pre-check (is_webhook_url_safe) and the HTTP client
    (make_safe_async_client) are both patched so no network I/O occurs.
    """

    def _make_repo(self, url: str) -> MagicMock:
        repo = MagicMock()
        repo.async_get_subscription_by_id = AsyncMock(return_value=_make_sub(url))
        repo.async_log_delivery = AsyncMock(return_value=_make_delivery())
        return repo

    def _make_user(self) -> dict:
        return {"user_id": 42}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "private_url",
        [
            "http://127.0.0.1/hook",
            "https://169.254.169.254/latest/meta-data/",
        ],
    )
    async def test_blocks_private_ip_urls(self, private_url: str) -> None:
        """send_test_webhook must return 400 when the stored URL resolves to a private IP."""
        from app.api.exceptions import APIException
        from app.api.routers.webhooks import send_test_webhook

        repo = self._make_repo(private_url)

        with patch(
            "app.api.routers.webhooks.is_webhook_url_safe",
            return_value=(False, "Private or reserved IP address"),
        ):
            with pytest.raises(APIException) as exc_info:
                await send_test_webhook(
                    webhook_id=1,
                    user=self._make_user(),
                    webhook_repo=repo,
                )

        assert exc_info.value.status_code == 400
        assert "SSRF" in str(exc_info.value.message) or "safety" in str(exc_info.value.message)

    @pytest.mark.asyncio
    async def test_allows_public_url(self) -> None:
        """send_test_webhook must succeed when the URL passes the SSRF check."""
        from app.api.routers.webhooks import send_test_webhook

        public_url = "https://hooks.example.com/notify"
        repo = self._make_repo(public_url)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        @asynccontextmanager
        async def _mock_safe_client(**_kwargs):
            yield mock_client

        with (
            patch(
                "app.api.routers.webhooks.is_webhook_url_safe",
                return_value=(True, None),
            ),
            patch(
                "app.api.routers.webhooks.make_safe_async_client",
                side_effect=_mock_safe_client,
            ),
        ):
            result = await send_test_webhook(
                webhook_id=1,
                user=self._make_user(),
                webhook_repo=repo,
            )

        assert result["success"] is True
        repo.async_log_delivery.assert_awaited_once()
