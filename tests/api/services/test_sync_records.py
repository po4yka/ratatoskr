"""Tests for sync service data operations: collect, paginate, get_full, get_delta, apply_changes."""

from datetime import datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.models.responses import SyncEntityEnvelope
from app.api.services.sync import SyncEntityAdapter, SyncEntityAdapterContext
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


class TestCollectRecords:
    """Test _collect_records method."""

    @pytest.mark.asyncio
    async def test_collect_records_all_types(self, sync_service):
        """Test collecting all entity types."""
        # Mock user
        sync_service._user_repo.async_get_user_by_telegram_id = AsyncMock(
            return_value={"telegram_user_id": 123, "username": "test", "server_version": 1}
        )

        # Mock requests
        sync_service._request_repo.async_get_all_for_user = AsyncMock(
            return_value=[
                {
                    "id": "req-1",
                    "type": "url",
                    "status": "completed",
                    "server_version": 2,
                    "is_deleted": False,
                }
            ]
        )

        # Mock summaries
        sync_service._summary_repo.async_get_all_for_user = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "request": 1,
                    "lang": "en",
                    "server_version": 3,
                    "is_deleted": False,
                }
            ]
        )

        # Mock crawl results
        sync_service._crawl_repo.async_get_all_for_user = AsyncMock(
            return_value=[
                {
                    "id": 10,
                    "request": 1,
                    "source_url": "http://test.com",
                    "server_version": 4,
                    "is_deleted": False,
                }
            ]
        )

        # Mock LLM calls
        sync_service._llm_repo.async_get_all_for_user = AsyncMock(
            return_value=[
                {
                    "id": 20,
                    "request": 1,
                    "provider": "openrouter",
                    "server_version": 5,
                    "is_deleted": False,
                }
            ]
        )

        sync_service._collector._aux_read_port.get_highlights_for_user = AsyncMock(return_value=[])

        records = await sync_service._collector.collect_records(123)

        assert len(records) == 5
        assert records[0].entity_type == "user"
        assert records[1].entity_type == "request"
        assert records[2].entity_type == "summary"
        assert records[3].entity_type == "crawl_result"
        assert records[4].entity_type == "llm_call"

    @pytest.mark.asyncio
    async def test_collect_records_no_user(self, sync_service):
        """Test collecting when user not found."""
        sync_service._user_repo.async_get_user_by_telegram_id = AsyncMock(return_value=None)
        sync_service._request_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._summary_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._crawl_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._llm_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._collector._aux_read_port.get_highlights_for_user = AsyncMock(return_value=[])

        records = await sync_service._collector.collect_records(123)

        assert len(records) == 0

    @pytest.mark.asyncio
    async def test_collect_records_pushes_since_into_repositories(self, sync_service):
        """Incremental sync pushes the cursor into every repo/aux query so the DB
        filters server_version > since instead of returning the whole history (audit #2)."""
        sync_service._user_repo.async_get_user_by_telegram_id = AsyncMock(return_value=None)
        sync_service._request_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._summary_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._crawl_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._llm_repo.async_get_all_for_user = AsyncMock(return_value=[])
        aux = sync_service._collector._aux_read_port
        aux.get_highlights_for_user = AsyncMock(return_value=[])
        aux.get_tags_for_user = AsyncMock(return_value=[])
        aux.get_summary_tags_for_user = AsyncMock(return_value=[])

        await sync_service._collector.collect_records(123, since=42)

        sync_service._request_repo.async_get_all_for_user.assert_awaited_once_with(123, since=42)
        sync_service._summary_repo.async_get_all_for_user.assert_awaited_once_with(123, since=42)
        sync_service._crawl_repo.async_get_all_for_user.assert_awaited_once_with(123, since=42)
        sync_service._llm_repo.async_get_all_for_user.assert_awaited_once_with(123, since=42)
        aux.get_highlights_for_user.assert_awaited_once_with(123, since=42)
        aux.get_tags_for_user.assert_awaited_once_with(123, since=42)
        aux.get_summary_tags_for_user.assert_awaited_once_with(123, since=42)

    @pytest.mark.asyncio
    async def test_collect_records_since_zero_is_full_read(self, sync_service):
        """The first sync (since=0) still issues a full read -- since=0 forwarded, no filter."""
        sync_service._user_repo.async_get_user_by_telegram_id = AsyncMock(return_value=None)
        sync_service._request_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._summary_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._crawl_repo.async_get_all_for_user = AsyncMock(return_value=[])
        sync_service._llm_repo.async_get_all_for_user = AsyncMock(return_value=[])
        aux = sync_service._collector._aux_read_port
        aux.get_highlights_for_user = AsyncMock(return_value=[])
        aux.get_tags_for_user = AsyncMock(return_value=[])
        aux.get_summary_tags_for_user = AsyncMock(return_value=[])

        await sync_service._collector.collect_records(123)

        sync_service._request_repo.async_get_all_for_user.assert_awaited_once_with(123, since=0)

    @pytest.mark.asyncio
    async def test_fake_sync_entity_adapter_collects_and_serializes(
        self, mock_config, mock_session_manager
    ):
        """A new entity type can be added through an adapter without changing collector code."""

        async def collect_fake(
            context: SyncEntityAdapterContext,
            user_id: int,
        ) -> list[dict[str, object]]:
            _ = context
            return [
                {
                    "id": f"fake-{user_id}",
                    "server_version": 9,
                    "updated_at": "2026-05-21T00:00:00Z",
                    "payload": {"name": "Fake"},
                }
            ]

        def serialize_fake(_serializer, row: dict[str, object]) -> SyncEntityEnvelope:
            return SyncEntityEnvelope.model_validate(
                {
                    "entity_type": "fake",
                    "id": str(row["id"]),
                    "server_version": cast("int", row["server_version"]),
                    "updated_at": str(row["updated_at"]),
                    "payload": cast("dict[str, object]", row["payload"]),
                }
            )

        async def max_fake(
            context: SyncEntityAdapterContext,
            user_id: int,
        ) -> int:
            _ = context, user_id
            return 9

        fake_adapter = SyncEntityAdapter(
            entity_type="fake",
            collect_records=collect_fake,
            serialize_record=serialize_fake,
            max_server_version=max_fake,
        )
        service = SyncService(
            mock_config,
            mock_session_manager,
            entity_adapters=(fake_adapter,),
        )

        records = await service._collector.collect_records(123)

        assert len(records) == 1
        assert records[0].entity_type == "fake"
        assert records[0].payload == {"name": "Fake"}
        assert await service.get_max_server_version(123) == 9


class TestPaginateRecords:
    """Test _paginate_records method."""

    def test_paginate_records_first_page(self, sync_service):
        """Test paginating first page."""
        records = [
            make_sync_envelope(entity_id=i, server_version=i) for i in range(1, 11)
        ]  # 10 records, versions 1-10

        page, has_more, _next_since = sync_service._collector.paginate_records(
            records, since=0, limit=5
        )

        assert len(page) == 5
        assert has_more is True
        assert _next_since == 5

    def test_paginate_records_keeps_same_version_group_atomic(self, sync_service):
        records = [
            make_sync_envelope(entity_type="request", entity_id=1, server_version=10),
            make_sync_envelope(entity_type="summary", entity_id=1, server_version=10),
            make_sync_envelope(entity_type="crawl_result", entity_id=1, server_version=10),
            make_sync_envelope(entity_type="llm_call", entity_id=1, server_version=11),
        ]

        page, has_more, _next_since = sync_service._collector.paginate_records(
            records, since=0, limit=2
        )

        assert [(item.entity_type, item.id) for item in page] == [
            ("request", 1),
            ("summary", 1),
            ("crawl_result", 1),
        ]
        assert has_more is True
        assert _next_since == 10
        next_page, next_has_more, next_since = sync_service._collector.paginate_records(
            records, since=_next_since or 0, limit=2
        )
        assert [(item.entity_type, item.id) for item in next_page] == [("llm_call", 1)]
        assert next_has_more is False
        assert next_since == 11

    def test_paginate_records_last_page(self, sync_service):
        """Test paginating last page."""
        records = [
            make_sync_envelope(entity_id=i, server_version=i) for i in range(1, 4)
        ]  # 3 records

        page, has_more, _next_since = sync_service._collector.paginate_records(
            records, since=0, limit=5
        )

        assert len(page) == 3
        assert has_more is False
        assert _next_since == 3

    def test_paginate_records_with_since(self, sync_service):
        """Test paginating with since cursor."""
        records = [make_sync_envelope(entity_id=i, server_version=i) for i in range(1, 11)]

        page, has_more, _next_since = sync_service._collector.paginate_records(
            records, since=5, limit=3
        )

        assert len(page) == 3
        assert all(r.server_version > 5 for r in page)
        assert has_more is True

    def test_paginate_records_empty(self, sync_service):
        """Test paginating with no records."""
        records = []

        page, has_more, _next_since = sync_service._collector.paginate_records(
            records, since=0, limit=5
        )

        assert len(page) == 0
        assert has_more is False
        assert _next_since == 0


class TestGetFull:
    """Test get_full method."""

    @pytest.mark.asyncio
    async def test_get_full_success(self, sync_service):
        """Test full sync retrieval."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "chunk_limit": 100,
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }

        with patch.object(
            sync_service,
            "_load_session",
            new_callable=AsyncMock,
            return_value=session_payload,
        ):
            with patch.object(
                sync_service._collector, "collect_records", new_callable=AsyncMock
            ) as mock_collect:
                mock_collect.return_value = [
                    make_sync_envelope(entity_id=i, server_version=i) for i in range(1, 6)
                ]

                result = await sync_service.get_full(
                    session_id="test-session", user_id=123, client_id="test-client", limit=10
                )

                assert result.session_id == "test-session"
                assert len(result.items) == 5
                assert result.has_more is False
                assert result.pagination.total == 5

    @pytest.mark.asyncio
    async def test_get_full_with_pagination(self, sync_service):
        """Test full sync with pagination."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "chunk_limit": 5,
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }

        with patch.object(
            sync_service,
            "_load_session",
            new_callable=AsyncMock,
            return_value=session_payload,
        ):
            with patch.object(
                sync_service._collector, "collect_records", new_callable=AsyncMock
            ) as mock_collect:
                # 200+ records to ensure pagination with limit=100
                mock_collect.return_value = [
                    make_sync_envelope(entity_id=i, server_version=i) for i in range(1, 151)
                ]

                # Use limit=50 to override session chunk_limit
                result = await sync_service.get_full(
                    session_id="test-session", user_id=123, client_id="test-client", limit=50
                )

                assert len(result.items) == 50
                assert result.has_more is True
                assert result.next_since == 50

    @pytest.mark.asyncio
    async def test_get_full_pushes_session_cursor_into_collection(self, sync_service):
        """get_full forwards the session's next_since to collect_records so a
        full-sync chunk reads only rows past the cursor (audit #2)."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "chunk_limit": 100,
            "next_since": 9,
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }
        with patch.object(
            sync_service, "_load_session", new_callable=AsyncMock, return_value=session_payload
        ):
            with patch.object(
                sync_service._collector,
                "collect_records",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_collect:
                await sync_service.get_full(
                    session_id="test-session", user_id=123, client_id="test-client", limit=10
                )

        mock_collect.assert_awaited_once_with(123, since=9)

    @pytest.mark.asyncio
    async def test_get_full_advances_session_cursor(self, sync_service):
        """Repeated full-sync chunks for one session must not replay the first page."""
        sync_service.cfg.sync.min_limit = 1
        session = await sync_service.start_session(
            user_id=123,
            client_id="test-client",
            limit=2,
        )
        records = [make_sync_envelope(entity_id=i, server_version=i) for i in range(1, 5)]

        with patch.object(
            sync_service._collector,
            "collect_records",
            new_callable=AsyncMock,
            return_value=records,
        ):
            first = await sync_service.get_full(
                session_id=session.session_id,
                user_id=123,
                client_id="test-client",
                limit=None,
            )
            second = await sync_service.get_full(
                session_id=session.session_id,
                user_id=123,
                client_id="test-client",
                limit=None,
            )

        assert [item.id for item in first.items] == [1, 2]
        assert first.has_more is True
        assert first.next_since == 2
        assert [item.id for item in second.items] == [3, 4]
        assert second.has_more is False
        assert second.next_since == 4


class TestGetDelta:
    """Test get_delta method."""

    @pytest.mark.asyncio
    async def test_get_delta_success(self, sync_service):
        """Test delta sync retrieval."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "chunk_limit": 100,
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }

        with patch.object(
            sync_service,
            "_load_session",
            new_callable=AsyncMock,
            return_value=session_payload,
        ):
            with patch.object(
                sync_service._collector, "collect_records", new_callable=AsyncMock
            ) as mock_collect:
                mock_collect.return_value = [
                    make_sync_envelope(entity_id=i, server_version=i, deleted_at=None)
                    for i in range(5, 8)
                ]

                result = await sync_service.get_delta(
                    session_id="test-session",
                    user_id=123,
                    client_id="test-client",
                    since=4,
                    limit=10,
                )

                assert result.session_id == "test-session"
                assert result.since == 4
                assert len(result.created) == 3
                assert len(result.updated) == 0
                assert len(result.deleted) == 0

    @pytest.mark.asyncio
    async def test_get_delta_pushes_since_into_collection(self, sync_service):
        """get_delta forwards the client's cursor to collect_records so only rows
        changed past it are read, not the whole history (audit #2)."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "chunk_limit": 100,
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }
        with patch.object(
            sync_service, "_load_session", new_callable=AsyncMock, return_value=session_payload
        ):
            with patch.object(
                sync_service._collector,
                "collect_records",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_collect:
                await sync_service.get_delta(
                    session_id="test-session",
                    user_id=123,
                    client_id="test-client",
                    since=7,
                    limit=10,
                )

        mock_collect.assert_awaited_once_with(123, since=7)

    @pytest.mark.asyncio
    async def test_get_delta_with_deletions(self, sync_service):
        """Test delta sync with deleted items."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "chunk_limit": 100,
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }

        with patch.object(
            sync_service,
            "_load_session",
            new_callable=AsyncMock,
            return_value=session_payload,
        ):
            with patch.object(
                sync_service._collector, "collect_records", new_callable=AsyncMock
            ) as mock_collect:
                deleted_time = now.isoformat() + "Z"
                mock_collect.return_value = [
                    make_sync_envelope(entity_id=5, server_version=5, deleted_at=None),
                    make_sync_envelope(entity_id=6, server_version=6, deleted_at=deleted_time),
                ]

                result = await sync_service.get_delta(
                    session_id="test-session",
                    user_id=123,
                    client_id="test-client",
                    since=4,
                    limit=10,
                )

                assert len(result.created) == 1
                assert len(result.deleted) == 1
                assert result.deleted[0].id == 6


class TestApplyChanges:
    """Test apply_changes method."""

    @pytest.mark.asyncio
    async def test_apply_changes_unsupported_entity(self, sync_service):
        """Test applying changes with unsupported entity type."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }

        from app.api.models.requests import SyncApplyItem

        changes = [
            SyncApplyItem(
                entity_type="request",  # Unsupported
                id=1,
                action="update",
                last_seen_version=1,
                payload={},
            )
        ]

        with patch.object(
            sync_service,
            "_load_session",
            new_callable=AsyncMock,
            return_value=session_payload,
        ):
            result = await sync_service.apply_changes(
                session_id="test-session", user_id=123, client_id="test-client", changes=changes
            )

            assert len(result.results) == 1
            assert result.results[0].status == "invalid"
            assert result.results[0].error_code == "UNSUPPORTED_ENTITY"

    @pytest.mark.asyncio
    async def test_apply_changes_summary_success(self, sync_service):
        """Test applying summary changes successfully."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }

        from app.api.models.requests import SyncApplyItem

        changes = [
            SyncApplyItem(
                entity_type="summary",
                id=1,
                action="update",
                last_seen_version=5,
                payload={"is_read": True},
            )
        ]

        sync_service._summary_repo.async_get_summary_for_sync_apply = AsyncMock(
            return_value={"id": 1, "server_version": 5, "is_read": False}
        )
        sync_service._summary_repo.async_apply_sync_change = AsyncMock(return_value=6)

        with patch.object(
            sync_service,
            "_load_session",
            new_callable=AsyncMock,
            return_value=session_payload,
        ):
            result = await sync_service.apply_changes(
                session_id="test-session", user_id=123, client_id="test-client", changes=changes
            )

            assert len(result.results) == 1
            assert result.results[0].status == "applied"
            assert result.results[0].server_version == 6

    @pytest.mark.asyncio
    async def test_apply_changes_summary_conflict(self, sync_service):
        """Test applying summary changes with version conflict."""
        now = datetime.now(UTC)
        session_payload = {
            "session_id": "test-session",
            "user_id": 123,
            "client_id": "test-client",
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }

        from app.api.models.requests import SyncApplyItem

        changes = [
            SyncApplyItem(
                entity_type="summary",
                id=1,
                action="update",
                last_seen_version=5,
                payload={"is_read": True},
            )
        ]

        sync_service._summary_repo.async_get_summary_for_sync_apply = AsyncMock(
            return_value={
                "id": 1,
                "server_version": 10,  # Newer version
                "is_read": True,
            }
        )

        with patch.object(
            sync_service,
            "_load_session",
            new_callable=AsyncMock,
            return_value=session_payload,
        ):
            result = await sync_service.apply_changes(
                session_id="test-session", user_id=123, client_id="test-client", changes=changes
            )

            assert len(result.results) == 1
            assert result.results[0].status == "conflict"
            assert result.results[0].server_version == 10
            assert result.results[0].error_code == "CONFLICT_VERSION"
            assert result.conflicts is not None
            assert len(result.conflicts) == 1

    @pytest.mark.asyncio
    async def test_apply_changes_fake_adapter_honors_idempotency_key(
        self,
        mock_config,
        mock_session_manager,
    ):
        """Direct service retries with the same idempotency key must not reapply adapters."""
        from app.api.models.requests import SyncApplyItem
        from app.api.models.responses import SyncApplyItemResult

        calls = 0

        async def collect_empty(
            context: SyncEntityAdapterContext,
            user_id: int,
        ) -> list[dict[str, object]]:
            _ = context, user_id
            return []

        def serialize_fake(_serializer, row: dict[str, object]) -> SyncEntityEnvelope:
            return SyncEntityEnvelope(
                entity_type="fake",
                id=str(row["id"]),
                server_version=cast("int", row["server_version"]),
                updated_at=str(row["updated_at"]),
            )

        async def apply_fake(
            context: SyncEntityAdapterContext,
            change: SyncApplyItem,
            user_id: int,
        ) -> SyncApplyItemResult:
            nonlocal calls
            _ = context, user_id
            calls += 1
            return SyncApplyItemResult(
                entity_type=change.entity_type,
                id=change.id,
                status="applied",
                server_version=11,
            )

        fake_adapter = SyncEntityAdapter(
            entity_type="fake",
            collect_records=collect_empty,
            serialize_record=serialize_fake,
            apply_change=apply_fake,
        )
        service = SyncService(
            mock_config,
            mock_session_manager,
            entity_adapters=(fake_adapter,),
        )
        session = await service.start_session(user_id=123, client_id="test-client", limit=None)
        changes = [
            SyncApplyItem(
                entity_type="fake",
                id="fake-1",
                action="update",
                last_seen_version=1,
                payload={"value": True},
            )
        ]

        first = await service.apply_changes(
            session_id=session.session_id,
            user_id=123,
            client_id="test-client",
            changes=changes,
            idempotency_key="fake-key",
        )
        second = await service.apply_changes(
            session_id=session.session_id,
            user_id=123,
            client_id="test-client",
            changes=changes,
            idempotency_key="fake-key",
        )

        assert first == second
        assert calls == 1


class TestApplySummaryChange:
    """Test _apply_summary_change method."""

    @pytest.mark.asyncio
    async def test_apply_summary_invalid_id(self, sync_service):
        """Test applying change with invalid ID."""
        from app.api.models.requests import SyncApplyItem

        change = SyncApplyItem(
            entity_type="summary", id="invalid", action="update", last_seen_version=5, payload={}
        )

        result = await sync_service._apply_service.apply_summary_change(change, 123)

        assert result.status == "invalid"
        assert result.error_code == "INVALID_ID"

    @pytest.mark.asyncio
    async def test_apply_summary_not_found(self, sync_service):
        """Test applying change when summary not found."""
        from app.api.models.requests import SyncApplyItem

        change = SyncApplyItem(
            entity_type="summary", id=999, action="update", last_seen_version=5, payload={}
        )

        sync_service._summary_repo.async_get_summary_for_sync_apply = AsyncMock(return_value=None)

        result = await sync_service._apply_service.apply_summary_change(change, 123)

        assert result.status == "invalid"
        assert result.error_code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_apply_summary_invalid_fields(self, sync_service):
        """Test applying change with invalid fields."""
        from app.api.models.requests import SyncApplyItem

        change = SyncApplyItem(
            entity_type="summary",
            id=1,
            action="update",
            last_seen_version=5,
            payload={"invalid_field": "value"},
        )

        sync_service._summary_repo.async_get_summary_for_sync_apply = AsyncMock(
            return_value={"id": 1, "server_version": 5}
        )

        result = await sync_service._apply_service.apply_summary_change(change, 123)

        assert result.status == "invalid"
        assert result.error_code == "INVALID_FIELDS"

    @pytest.mark.asyncio
    async def test_apply_summary_delete_action(self, sync_service):
        """Test applying delete action."""
        from app.api.models.requests import SyncApplyItem

        change = SyncApplyItem(
            entity_type="summary", id=1, action="delete", last_seen_version=5, payload=None
        )

        sync_service._summary_repo.async_get_summary_for_sync_apply = AsyncMock(
            return_value={"id": 1, "server_version": 5}
        )
        sync_service._summary_repo.async_apply_sync_change = AsyncMock(return_value=6)

        result = await sync_service._apply_service.apply_summary_change(change, 123)

        assert result.status == "applied"
        assert result.server_version == 6

        # Verify delete was called
        call_kwargs = sync_service._summary_repo.async_apply_sync_change.call_args[1]
        assert call_kwargs["is_deleted"] is True
        assert call_kwargs["deleted_at"] is not None

    @pytest.mark.asyncio
    async def test_apply_summary_update_is_read(self, sync_service):
        """Test updating is_read field."""
        from app.api.models.requests import SyncApplyItem

        change = SyncApplyItem(
            entity_type="summary",
            id=1,
            action="update",
            last_seen_version=5,
            payload={"is_read": True},
        )

        sync_service._summary_repo.async_get_summary_for_sync_apply = AsyncMock(
            return_value={"id": 1, "server_version": 5}
        )
        sync_service._summary_repo.async_apply_sync_change = AsyncMock(return_value=6)

        result = await sync_service._apply_service.apply_summary_change(change, 123)

        assert result.status == "applied"

        # Verify is_read was updated
        call_kwargs = sync_service._summary_repo.async_apply_sync_change.call_args[1]
        assert call_kwargs["is_read"] is True
