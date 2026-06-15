"""Tests for sync service misc/serialization: serialization edge cases, build responses, coerce_iso."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

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
    service = SyncService(mock_config, mock_session_manager)
    # Mock the repositories
    service._user_repo = MagicMock()
    service._request_repo = MagicMock()
    service._summary_repo = MagicMock()
    service._crawl_repo = MagicMock()
    service._llm_repo = MagicMock()
    return service


@pytest.fixture(autouse=True)
def clear_sync_service_fallback_state():
    """No-op: fallback sync session state is now instance-level (not module globals)."""
    return


class TestSerializationEdgeCases:
    """Test serialization edge cases for different entity types."""

    def test_serialize_request_deleted(self, sync_service):
        """Test serializing deleted request."""
        now = datetime.now(UTC)
        request_dict = {
            "id": "req-1",
            "type": "url",
            "server_version": 10,
            "is_deleted": True,
            "deleted_at": now,
            "updated_at": now,
        }

        envelope = sync_service._serializer.serialize_request(request_dict)

        assert envelope.entity_type == "request"
        assert envelope.request is None
        assert envelope.deleted_at is not None

    def test_serialize_summary_deleted(self, sync_service):
        """Test serializing deleted summary."""
        now = datetime.now(UTC)
        summary_dict = {
            "id": 1,
            "request": 1,
            "server_version": 10,
            "is_deleted": True,
            "deleted_at": now,
            "updated_at": now,
        }

        envelope = sync_service._serializer.serialize_summary(summary_dict)

        assert envelope.entity_type == "summary"
        assert envelope.summary is None
        assert envelope.deleted_at is not None

    def test_serialize_summary_request_as_none(self, sync_service):
        """Test serializing summary when request is None."""
        summary_dict = {
            "id": 1,
            "request": None,
            "lang": "en",
            "is_read": False,
            "server_version": 10,
            "is_deleted": False,
            "created_at": None,
            "updated_at": None,
        }

        envelope = sync_service._serializer.serialize_summary(summary_dict)

        assert envelope.summary["request_id"] is None

    def test_serialize_crawl_result_deleted(self, sync_service):
        """Test serializing deleted crawl result."""
        now = datetime.now(UTC)
        crawl_dict = {
            "id": 1,
            "request": 1,
            "server_version": 10,
            "is_deleted": True,
            "deleted_at": now,
            "updated_at": now,
        }

        envelope = sync_service._serializer.serialize_crawl_result(crawl_dict)

        assert envelope.entity_type == "crawl_result"
        assert envelope.crawl_result is None
        assert envelope.deleted_at is not None

    def test_serialize_crawl_result_request_as_dict(self, sync_service):
        """Test serializing crawl result with request as dict."""
        crawl_dict = {
            "id": 1,
            "request": {"id": 42, "type": "url"},
            "source_url": "http://test.com",
            "endpoint": "firecrawl",
            "server_version": 10,
            "is_deleted": False,
            "updated_at": None,
        }

        envelope = sync_service._serializer.serialize_crawl_result(crawl_dict)

        assert envelope.crawl_result["request_id"] == 42

    def test_serialize_llm_call_deleted(self, sync_service):
        """Test serializing deleted LLM call."""
        now = datetime.now(UTC)
        call_dict = {
            "id": 1,
            "request": 1,
            "server_version": 10,
            "is_deleted": True,
            "deleted_at": now,
            "created_at": now,
            "updated_at": now,
        }

        envelope = sync_service._serializer.serialize_llm_call(call_dict)

        assert envelope.entity_type == "llm_call"
        assert envelope.llm_call is None
        assert envelope.deleted_at is not None

    def test_serialize_llm_call_request_as_dict(self, sync_service):
        """Test serializing LLM call with request as dict."""
        call_dict = {
            "id": 1,
            "request": {"id": 42, "type": "url"},
            "provider": "openrouter",
            "model": "gpt-4",
            "status": "completed",
            "server_version": 10,
            "is_deleted": False,
            "created_at": None,
            "updated_at": None,
        }

        envelope = sync_service._serializer.serialize_llm_call(call_dict)

        assert envelope.llm_call["request_id"] == 42


class TestBuildResponses:
    """Test _build_full and _build_delta methods."""

    def test_build_full_response(self, sync_service):
        """Test building full sync response."""
        records = [make_sync_envelope(entity_id=i, server_version=i) for i in range(1, 4)]

        response = sync_service._build_full(
            session_id="test-session", records=records, has_more=False, next_since=3, limit=100
        )

        assert response.session_id == "test-session"
        assert len(response.items) == 3
        assert response.has_more is False
        assert response.next_since == 3
        assert response.pagination.total == 3
        assert response.pagination.limit == 100

    def test_build_delta_response(self, sync_service):
        """Test building delta sync response."""
        now = datetime.now(UTC)
        records = [
            make_sync_envelope(entity_id=5, server_version=5, deleted_at=None),
            make_sync_envelope(entity_id=6, server_version=6, deleted_at=now.isoformat() + "Z"),
        ]

        response = sync_service._build_delta(
            session_id="test-session",
            since=4,
            records=records,
            has_more=False,
            next_since=6,
            limit=100,
        )

        assert response.session_id == "test-session"
        assert response.since == 4
        assert len(response.created) == 1
        assert len(response.updated) == 0
        assert len(response.deleted) == 1


class TestCoerceIsoEdgeCases:
    """Additional edge cases for _coerce_iso."""

    def test_coerce_iso_with_string_datetime(self, sync_service):
        """Test with datetime that is actually a string object."""
        # This tests the isinstance(dt_value, str) branch
        result = sync_service._serializer._coerce_iso("2024-01-15T10:30:00+00:00")
        assert "2024-01-15T10:30:00" in result
        assert result.endswith("Z")

    def test_coerce_iso_with_malformed_string(self, sync_service):
        """Test with completely malformed string."""
        result = sync_service._serializer._coerce_iso("not a datetime at all!!!")
        # Should fallback to current time
        assert "T" in result
        assert result.endswith("Z")

    def test_coerce_iso_with_numeric_value(self, sync_service):
        """Test with numeric value (edge case)."""
        result = sync_service._serializer._coerce_iso(12345)
        # Should fallback to current time
        assert "T" in result
        assert result.endswith("Z")
