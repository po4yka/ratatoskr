"""Tests for sync service session management: resolve_limit, store/load/start session."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.exceptions import (
    SyncSessionExpiredError,
    SyncSessionForbiddenError,
    SyncSessionNotFoundError,
)
from app.api.models.responses import SyncEntityEnvelope
from app.api.services.sync_service import SyncService
from app.core.time_utils import UTC


def make_sync_envelope(
    entity_type: str = "request",
    entity_id: int = 1,
    server_version: int = 1,
    deleted_at: str | None = None,
) -> SyncEntityEnvelope:
    """Helper to create SyncEntityEnvelope instances for testing."""
    return SyncEntityEnvelope(
        entity_type=entity_type,
        id=entity_id,
        server_version=server_version,
        updated_at=datetime.now(UTC).isoformat() + "Z",
        deleted_at=deleted_at,
    )


@pytest.fixture
def mock_config():
    """Create mock AppConfig."""
    cfg = MagicMock()
    cfg.sync.expiry_hours = 2
    cfg.sync.default_limit = 200
    cfg.sync.min_limit = 10
    cfg.sync.max_limit = 500
    cfg.redis.prefix = "test"
    cfg.redis.enabled = False
    return cfg


@pytest.fixture
def mock_session_manager():
    """Create mock DatabaseSessionManager."""
    return MagicMock()


@pytest.fixture
def sync_service(mock_config, mock_session_manager):
    """Create SyncService instance with mocked dependencies."""
    return SyncService(
        mock_config,
        mock_session_manager,
        user_repository=MagicMock(),
        request_repository=MagicMock(),
        summary_repository=MagicMock(),
        crawl_result_repository=MagicMock(),
        llm_repository=MagicMock(),
    )


@pytest.fixture(autouse=True)
def clear_sync_service_fallback_state():
    """No-op: fallback sync session state is now instance-level (not module globals)."""
    return


class TestResolveLimit:
    """Test _resolve_limit method."""

    def test_resolve_limit_with_none(self, sync_service):
        """Test with None returns default limit."""
        result = sync_service._resolve_limit(None)
        assert result == 200  # default_limit

    def test_resolve_limit_below_min(self, sync_service):
        """Test with value below min returns min limit."""
        result = sync_service._resolve_limit(5)
        assert result == 10  # min_limit

    def test_resolve_limit_above_max(self, sync_service):
        """Test with value above max returns max limit."""
        result = sync_service._resolve_limit(1000)
        assert result == 500  # max_limit

    def test_resolve_limit_within_range(self, sync_service):
        """Test with valid value returns that value."""
        result = sync_service._resolve_limit(100)
        assert result == 100


class TestStoreSession:
    """Test _store_session method."""

    @pytest.mark.asyncio
    async def test_store_session_redis_available(self, sync_service, mock_config):
        """Test storing session when Redis is available."""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()

        with patch(
            "app.api.services.sync_service.get_redis",
            new=AsyncMock(return_value=mock_redis),
        ):
            payload = {
                "session_id": "test-session",
                "user_id": 123,
                "client_id": "test-client",
            }

            await sync_service._store_session(payload)

            mock_redis.set.assert_called_once()
            call_args = mock_redis.set.call_args
            assert "test:sync:session:test-session" in call_args[0][0]
            assert json.loads(call_args[0][1]) == payload
            assert call_args[1]["ex"] == int(mock_config.sync.expiry_hours * 3600)

    @pytest.mark.asyncio
    async def test_store_session_redis_unavailable_fallback(self, sync_service):
        """Test fallback to in-memory when Redis unavailable."""
        with patch("app.api.services.sync_service.get_redis", return_value=None):
            payload = {
                "session_id": "test-session-fallback",
                "user_id": 456,
                "client_id": "test-client",
            }

            # Reset the warning flag to test logging (now instance-level)
            sync_service._redis_warning_logged = False

            await sync_service._store_session(payload)

            # Check in-memory storage (now instance-level)
            assert "test-session-fallback" in sync_service._sync_sessions
            assert sync_service._sync_sessions["test-session-fallback"] == payload


class TestLoadSession:
    """Test _load_session method."""

    @pytest.mark.asyncio
    async def test_load_session_redis_success(self, sync_service):
        """Test loading session from Redis successfully."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=1)
        payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(payload))
        mock_redis.ttl = AsyncMock(return_value=3600)

        with patch("app.api.services.sync_service.get_redis", return_value=mock_redis):
            result = await sync_service._load_session("test-session", 123, "test-client")

            assert result == payload
            mock_redis.get.assert_called_once()
            mock_redis.ttl.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_session_redis_not_found(self, sync_service):
        """Test loading non-existent session from Redis."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.ttl = AsyncMock(return_value=-2)

        with patch("app.api.services.sync_service.get_redis", return_value=mock_redis):
            with pytest.raises(SyncSessionNotFoundError) as exc_info:
                await sync_service._load_session("missing-session", 123, "test-client")

            assert "missing-session" in str(exc_info.value.details.get("session_id", ""))

    @pytest.mark.asyncio
    async def test_load_session_fallback_not_found(self, sync_service):
        """Test loading from in-memory fallback when session not found."""
        with patch("app.api.services.sync_service.get_redis", return_value=None):
            with pytest.raises(SyncSessionNotFoundError):
                await sync_service._load_session("missing-session", 123, "test-client")

    @pytest.mark.asyncio
    async def test_load_session_forbidden_wrong_user(self, sync_service):
        """Test loading session with mismatched user_id."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=1)
        payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(payload))
        mock_redis.ttl = AsyncMock(return_value=3600)

        with patch("app.api.services.sync_service.get_redis", return_value=mock_redis):
            with pytest.raises(SyncSessionForbiddenError):
                await sync_service._load_session("test-session", 999, "test-client")

    @pytest.mark.asyncio
    async def test_load_session_forbidden_wrong_client(self, sync_service):
        """Test loading session with mismatched client_id."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=1)
        payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(payload))
        mock_redis.ttl = AsyncMock(return_value=3600)

        with patch("app.api.services.sync_service.get_redis", return_value=mock_redis):
            with pytest.raises(SyncSessionForbiddenError):
                await sync_service._load_session("test-session", 123, "wrong-client")

    @pytest.mark.asyncio
    async def test_load_session_expired(self, sync_service):
        """Test loading expired session."""
        now = datetime.now(UTC)
        expires_at = now - timedelta(hours=1)  # Expired
        payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(payload))
        mock_redis.ttl = AsyncMock(return_value=100)

        with patch("app.api.services.sync_service.get_redis", return_value=mock_redis):
            with pytest.raises(SyncSessionExpiredError) as exc_info:
                await sync_service._load_session("test-session", 123, "test-client")

            assert "test-session" in str(exc_info.value.details.get("session_id", ""))

    @pytest.mark.asyncio
    async def test_load_session_fallback_expired_entry_is_removed(self, sync_service):
        """Test expired fallback sessions are evicted after access."""
        expires_at = datetime.now(UTC) - timedelta(minutes=1)
        sync_service._sync_sessions["expired-session"] = {
            "session_id": "expired-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }

        with patch("app.api.services.sync_service.get_redis", new=AsyncMock(return_value=None)):
            with pytest.raises(SyncSessionExpiredError):
                await sync_service._load_session("expired-session", 123, "test-client")

        assert "expired-session" not in sync_service._sync_sessions


class TestStartSession:
    """Test start_session method."""

    @pytest.mark.asyncio
    async def test_start_session_success(self, sync_service, mock_config):
        """Test starting a new session successfully."""
        with patch.object(
            sync_service, "_store_session", new_callable=AsyncMock
        ) as mock_store:
            result = await sync_service.start_session(
                user_id=123, client_id="test-client", limit=100
            )

            assert result.session_id.startswith("sync-")
            assert result.default_limit == 200
            assert result.max_limit == 500
            assert result.last_issued_since == 0
            mock_store.assert_called_once()

            # Verify stored payload
            stored_payload = mock_store.call_args[0][0]
            assert stored_payload["user_id"] == 123
            assert stored_payload["client_id"] == "test-client"
            assert stored_payload["chunk_limit"] == 100

    @pytest.mark.asyncio
    async def test_start_session_with_none_limit(self, sync_service):
        """Test starting session with None limit uses default."""
        with patch.object(
            sync_service, "_store_session", new_callable=AsyncMock
        ) as mock_store:
            result = await sync_service.start_session(
                user_id=123, client_id="test-client", limit=None
            )

            stored_payload = mock_store.call_args[0][0]
            assert stored_payload["chunk_limit"] == 200  # default_limit
