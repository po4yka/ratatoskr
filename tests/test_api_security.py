"""
Security tests for Mobile API.

Tests critical security fixes:
1. Telegram authentication verification
2. CORS configuration
3. Authorization checks
4. JWT secret validation
"""

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# These tests require optional 'api' extras (pyjwt, fastapi).
pytest.importorskip("jwt", reason="PyJWT not installed (install with: pip install .[api])")


class TestTelegramAuth:
    """Test Telegram authentication verification."""

    def test_telegram_auth_verifies_hash(self):
        """Test that Telegram auth hash is properly verified."""
        from app.api.routers.auth.telegram import verify_telegram_auth

        # This should raise HTTPException with invalid hash
        with pytest.raises(Exception) as exc_info:
            verify_telegram_auth(
                user_id=123456789,
                auth_hash="invalid_hash",
                auth_date=int(time.time()),
                username="testuser",
            )

        assert exc_info.value.status_code == 401
        assert "Invalid authentication hash" in str(exc_info.value.message)

    def test_telegram_auth_checks_timestamp(self):
        """Test that expired timestamps are rejected."""
        from app.api.routers.auth.telegram import verify_telegram_auth

        # Timestamp from 1 hour ago (should fail)
        old_timestamp = int(time.time()) - 3600

        with pytest.raises(Exception) as exc_info:
            verify_telegram_auth(
                user_id=123456789,
                auth_hash="any_hash",
                auth_date=old_timestamp,
                username="testuser",
            )

        assert exc_info.value.status_code == 401
        assert "expired" in str(exc_info.value.message).lower()

    def test_telegram_auth_requires_whitelist(self):
        """Test that users must be in whitelist."""
        from app.api.routers.auth.telegram import verify_telegram_auth
        from app.config import Config

        # Create valid hash for non-whitelisted user
        user_id = 999999999  # Not in whitelist
        auth_date = int(time.time())

        # Build data check string
        data_check_arr = [f"auth_date={auth_date}", f"id={user_id}", "username=hacker"]
        data_check_arr.sort()
        data_check_string = "\n".join(data_check_arr)

        # Compute valid hash
        bot_token = Config.get("BOT_TOKEN", "test_token")
        secret_key = hashlib.sha256(bot_token.encode()).digest()
        valid_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        # Even with valid hash, should fail if not in whitelist
        with pytest.raises(Exception) as exc_info:
            verify_telegram_auth(
                user_id=user_id,
                auth_hash=valid_hash,
                auth_date=auth_date,
                username="hacker",
            )

        assert exc_info.value.status_code == 403
        assert "not authorized" in str(exc_info.value.message).lower()


class TestCORSConfiguration:
    """Test CORS configuration values."""

    def test_cors_not_wildcard(self):
        """Test that CORS does not allow all origins by checking config values."""
        from app.config import Config

        # Get the raw CORS config - this doesn't require loading the full API
        allowed_origins = Config.get("ALLOWED_ORIGINS", "")
        origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]

        # If explicitly configured, should not contain wildcard
        if origins:
            assert "*" not in origins, "ALLOWED_ORIGINS should not contain wildcard '*'"

    def test_cors_allows_specific_origins_only(self):
        """Test that configured origins are specific, not wildcards."""
        from app.config import Config

        allowed_origins = Config.get("ALLOWED_ORIGINS", "")
        origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]

        # If explicitly configured, check that origins are specific
        for origin in origins:
            assert origin.startswith(("http://", "https://")), (
                f"Origin must start with http:// or https://: {origin}"
            )
            assert "*" not in origin, f"Origin should not contain wildcard: {origin}"


class TestAuthorizationChecks:
    """Test authorization checks on services."""

    @pytest.fixture
    def mock_user(self):
        """Mock authenticated user."""
        return {"user_id": 123456789, "username": "testuser"}

    @pytest.fixture
    def other_user(self):
        """Mock different user."""
        return {"user_id": 987654321, "username": "otheruser"}

    @pytest.mark.asyncio
    async def test_cannot_access_other_users_summary(self, mock_user, other_user):
        """The read-model use case denies access to a summary owned by another user.

        get_summary_by_id_for_user compares the stored summary's user_id to the
        requester and returns None when they differ; the router maps None to 404.
        """
        from app.application.use_cases.summary_read_model import SummaryReadModelUseCase

        owned_summary = {"id": 42, "user_id": mock_user["user_id"], "is_deleted": False}
        summary_repo = MagicMock()
        summary_repo.async_get_summary_by_id = AsyncMock(return_value=owned_summary)
        use_case = SummaryReadModelUseCase(
            summary_repo, MagicMock(), MagicMock(), MagicMock()
        )

        # The owner can read their own summary.
        assert (
            await use_case.get_summary_by_id_for_user(
                user_id=mock_user["user_id"], summary_id=42
            )
            == owned_summary
        )
        # A different user is denied (None -> 404 at the router).
        assert (
            await use_case.get_summary_by_id_for_user(
                user_id=other_user["user_id"], summary_id=42
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_cannot_access_other_users_request(self, mock_user, other_user):
        """Test that users cannot access each other's requests via service layer."""
        from app.application.services.request_service import RequestService
        from app.domain.exceptions.domain_exceptions import ResourceNotFoundError

        request_repo = MagicMock()
        request_repo.async_get_request_context = AsyncMock(return_value=None)
        request_repo.async_get_request_by_id = AsyncMock(
            return_value={"id": 100, "user_id": mock_user["user_id"], "status": "ok"}
        )

        service = RequestService(
            db=None,
            request_repository=request_repo,
            summary_repository=MagicMock(),
            crawl_result_repository=MagicMock(),
            llm_repository=MagicMock(),
        )

        with pytest.raises(ResourceNotFoundError):
            await service.get_request_by_id(
                user_id=other_user["user_id"],
                request_id=100,
            )


class TestJWTSecretValidation:
    """Test JWT secret validation."""

    def test_jwt_secret_required(self):
        """Test that JWT_SECRET_KEY must be configured."""
        from app.api.routers.auth import tokens

        with patch("app.api.routers.auth.tokens.Config.get", return_value=""):
            tokens._secret_key_holder[0] = None
            with pytest.raises(RuntimeError) as exc_info:
                tokens._get_secret_key()

        assert "JWT_SECRET_KEY" in str(exc_info.value)

    def test_jwt_secret_minimum_length(self):
        """Test that JWT_SECRET_KEY must be at least 32 characters."""
        from app.api.routers.auth import tokens

        with patch("app.api.routers.auth.tokens.Config.get", return_value="short"):
            tokens._secret_key_holder[0] = None
            with pytest.raises(RuntimeError) as exc_info:
                tokens._get_secret_key()

        assert "at least 32 characters" in str(exc_info.value)


class TestSecurityHeaders:
    """Test security headers and configurations."""

    def test_cors_middleware_not_permissive(self):
        """Test that CORS middleware is configured properly via config check."""
        from app.config import Config

        # Verify that if ALLOWED_ORIGINS is set, it doesn't contain permissive values
        allowed_origins = Config.get("ALLOWED_ORIGINS", "")

        # If configured, should not be overly permissive
        if allowed_origins:
            assert allowed_origins != "*", "ALLOWED_ORIGINS should not be wildcard"
            # Split and check each origin
            origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]
            for origin in origins:
                # Should not be a wildcard pattern
                assert not origin.endswith("*"), f"Origin should not use wildcard: {origin}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
