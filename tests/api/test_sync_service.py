"""Tests for sync service datetime serialization safety."""

import sys
from datetime import datetime
from unittest.mock import MagicMock

# Mock redis before importing sync_service
sys.modules["redis"] = MagicMock()
sys.modules["redis.asyncio"] = MagicMock()

from app.core.time_utils import UTC


class TestSyncServiceCoerceIso:
    """Test _coerce_iso helper handles various datetime inputs."""

    def _get_sync_service(self):
        """Create a SyncService instance for testing."""
        from app.api.services.sync_service import SyncService

        mock_cfg = MagicMock()
        mock_cfg.sync.expiry_hours = 1
        mock_cfg.sync.default_limit = 200
        mock_cfg.sync.min_limit = 1
        mock_cfg.sync.max_limit = 500
        mock_cfg.redis.prefix = "test"

        mock_session_manager = MagicMock()
        return SyncService(mock_cfg, mock_session_manager)

    def test_coerce_iso_with_datetime(self):
        """Test _coerce_iso with proper datetime object."""
        service = self._get_sync_service()
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = service._serializer._coerce_iso(dt)
        assert result == "2024-01-15T10:30:00+00:00Z"

    def test_coerce_iso_with_naive_datetime(self):
        """Test _coerce_iso with naive datetime (no timezone)."""
        service = self._get_sync_service()
        dt = datetime(2024, 1, 15, 10, 30, 0)
        result = service._serializer._coerce_iso(dt)
        assert "2024-01-15T10:30:00" in result

    def test_coerce_iso_with_iso_string(self):
        """Test _coerce_iso with ISO string input."""
        service = self._get_sync_service()
        iso_str = "2024-01-15T10:30:00Z"
        result = service._serializer._coerce_iso(iso_str)
        assert "2024-01-15T10:30:00" in result

    def test_coerce_iso_with_none(self):
        """Test _coerce_iso with None returns current time."""
        service = self._get_sync_service()
        result = service._serializer._coerce_iso(None)
        # Should return current time in ISO format
        assert result is not None
        assert "T" in result  # ISO format has T separator
        assert result.endswith("Z")

    def test_coerce_iso_with_invalid_string(self):
        """Test _coerce_iso with invalid string returns current time."""
        service = self._get_sync_service()
        result = service._serializer._coerce_iso("not-a-date")
        # Should fallback to current time
        assert result is not None
        assert "T" in result


class TestSyncServiceSerialization:
    """Test entity serialization handles edge cases."""

    def _get_sync_service(self):
        """Create a SyncService instance for testing."""
        from app.api.services.sync_service import SyncService

        mock_cfg = MagicMock()
        mock_cfg.sync.expiry_hours = 1
        mock_cfg.sync.default_limit = 200
        mock_cfg.sync.min_limit = 1
        mock_cfg.sync.max_limit = 500
        mock_cfg.redis.prefix = "test"

        mock_session_manager = MagicMock()
        return SyncService(mock_cfg, mock_session_manager)

    def test_serialize_request_with_none_dates(self):
        """Test _serialize_request handles None datetime fields."""
        service = self._get_sync_service()

        # Use dict instead of MagicMock since serialization now expects dicts
        request_dict = {
            "id": 1,
            "type": "url",
            "status": "completed",
            "input_url": "http://test.com",
            "normalized_url": "http://test.com",
            "correlation_id": "test-123",
            "server_version": 1000,
            "is_deleted": False,
            "created_at": None,  # None datetime
            "updated_at": None,  # None datetime
            "deleted_at": None,
        }

        envelope = service._serializer.serialize_request(request_dict)

        assert envelope.entity_type == "request"
        assert envelope.id == 1
        # Should not raise - _coerce_iso handles None
        assert envelope.updated_at is not None

    def test_serialize_summary_with_none_dates(self):
        """Test _serialize_summary handles None datetime fields."""
        service = self._get_sync_service()

        # Use dict instead of MagicMock since serialization now expects dicts
        summary_dict = {
            "id": 1,
            "request": 1,  # Flattened to just the ID
            "lang": "en",
            "is_read": False,
            "json_payload": {"title": "Test"},
            "server_version": 1000,
            "is_deleted": False,
            "created_at": None,  # None datetime
            "updated_at": None,  # None datetime
            "deleted_at": None,
        }

        envelope = service._serializer.serialize_summary(summary_dict)

        assert envelope.entity_type == "summary"
        assert envelope.id == 1
        # Should not raise - _coerce_iso handles None
        assert envelope.updated_at is not None

    def test_serialize_summary_with_request_dict(self):
        """Test _serialize_summary handles request as dict."""
        service = self._get_sync_service()

        summary_dict = {
            "id": 1,
            "request": {"id": 42, "type": "url"},  # Request as dict
            "lang": "en",
            "is_read": False,
            "json_payload": {"title": "Test"},
            "server_version": 1000,
            "is_deleted": False,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        }

        envelope = service._serializer.serialize_summary(summary_dict)

        assert envelope.entity_type == "summary"
        assert envelope.id == 1
        assert envelope.summary["request_id"] == 42

    def test_serialize_crawl_result_with_none_dates(self):
        """Test _serialize_crawl_result handles None datetime fields."""
        service = self._get_sync_service()

        # Use dict instead of MagicMock since serialization now expects dicts
        crawl_dict = {
            "id": 1,
            "request": 1,  # Flattened to just the ID
            "source_url": "http://test.com",
            "endpoint": "firecrawl",
            "http_status": 200,
            "metadata_json": {},
            "latency_ms": 100,
            "server_version": 1000,
            "is_deleted": False,
            "created_at": None,
            "updated_at": None,  # None datetime
            "deleted_at": None,
        }

        envelope = service._serializer.serialize_crawl_result(crawl_dict)

        assert envelope.entity_type == "crawl_result"
        assert envelope.id == 1
        # Should not raise - _coerce_iso handles None
        assert envelope.updated_at is not None

    def test_serialize_llm_call_with_none_dates(self):
        """Test _serialize_llm_call handles None datetime fields."""
        service = self._get_sync_service()

        # Use dict instead of MagicMock since serialization now expects dicts
        call_dict = {
            "id": 1,
            "request": 1,  # Flattened to just the ID
            "provider": "openrouter",
            "model": "gpt-4",
            "status": "completed",
            "tokens_prompt": 100,
            "tokens_completion": 50,
            "cost_usd": 0.01,
            "server_version": 1000,
            "is_deleted": False,
            "created_at": None,  # None datetime
            "updated_at": None,  # None datetime
            "deleted_at": None,
        }

        envelope = service._serializer.serialize_llm_call(call_dict)

        assert envelope.entity_type == "llm_call"
        assert envelope.id == 1
        # Should not raise - _coerce_iso handles None
        assert envelope.updated_at is not None

    def test_serialize_user_with_none_dates(self):
        """Test _serialize_user handles None datetime fields."""
        service = self._get_sync_service()

        user_dict = {
            "telegram_user_id": 123456,
            "username": "testuser",
            "is_owner": True,
            "preferences_json": {"theme": "dark"},
            "server_version": 1000,
            "created_at": None,
            "updated_at": None,
        }

        envelope = service._serializer.serialize_user(user_dict)

        assert envelope.entity_type == "user"
        assert envelope.id == 123456
        assert envelope.updated_at is not None
        assert envelope.preference["username"] == "testuser"
