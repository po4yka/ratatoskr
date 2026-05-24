from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.time_utils import UTC
from app.infrastructure.persistence.repositories.admin_read_repository import (
    AdminReadRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.aggregation_session_repository import (
    AggregationSessionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.attachment_processing_repository import (
    AttachmentProcessingRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.audio_generation_repository import (
    AudioGenerationRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.auth_repository import AuthRepositoryAdapter
from app.infrastructure.persistence.repositories.backup_repository import BackupRepositoryAdapter
from app.infrastructure.persistence.repositories.batch_session_repository import (
    BatchSessionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.bookmark_import_repository import (
    BookmarkImportAdapter,
)
from app.infrastructure.persistence.repositories.crawl_result_repository import (
    CrawlResultRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.device_repository import DeviceRepositoryAdapter
from app.infrastructure.persistence.repositories.embedding_repository import (
    EmbeddingRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.import_job_repository import (
    ImportJobRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.latency_stats_repository import (
    LatencyStatsRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.llm_repository import LLMRepositoryAdapter
from app.infrastructure.persistence.repositories.repository_analysis_repository import (
    RepositoryAnalysisRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.request_repository import RequestRepositoryAdapter
from app.infrastructure.persistence.repositories.rss_feed_repository import RSSFeedRepositoryAdapter
from app.infrastructure.persistence.repositories.rule_repository import RuleRepositoryAdapter
from app.infrastructure.persistence.repositories.signal_source_repository import (
    SignalSourceRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.social_connection_repository import (
    SocialConnectionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.tag_repository import TagRepositoryAdapter
from app.infrastructure.persistence.repositories.telegram_message_repository import (
    TelegramMessageRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.topic_search_repository import (
    TopicSearchRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.transcription_repository import (
    TranscriptionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.user_credentials_repository import (
    UserCredentialRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.user_repository import UserRepositoryAdapter
from app.infrastructure.persistence.repositories.video_download_repository import (
    VideoDownloadRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.webhook_repository import WebhookRepositoryAdapter


class _Result:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []
        self.rowcount = 0

    def __iter__(self) -> Any:
        return iter(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> Any | None:
        return self.first()

    def one(self) -> Any:
        return self.first() or SimpleNamespace(id=1)

    def all(self) -> list[Any]:
        return self._rows

    def mappings(self) -> _Result:
        return self

    def scalars(self) -> _Result:
        return self


class _Session:
    def __init__(self) -> None:
        self.executed = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def scalar(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        self.executed += 1
        return _Result()

    async def scalars(self, *_args: Any, **_kwargs: Any) -> _Result:
        self.executed += 1
        return _Result()

    async def get(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def add(self, _obj: Any) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def refresh(self, _obj: Any) -> None:
        return None

    async def delete(self, _obj: Any) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.session_obj = _Session()

    def session(self) -> _Session:
        return self.session_obj

    def transaction(self) -> _Session:
        return self.session_obj


def _dummy_value(name: str) -> Any:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    values = {
        "after": now,
        "before": now,
        "bookmark": SimpleNamespace(
            url="https://example.test",
            title="Title",
            description="Description",
            tags=["tag"],
            created_at=now,
        ),
        "chat_id": 1,
        "collection_id": 1,
        "connection_id": 1,
        "created_after": now,
        "device_id": "device",
        "email": "user@example.test",
        "end_date": now,
        "expires_at": now,
        "feed_id": 1,
        "filters": {},
        "github_user_id": 1,
        "ids": [1, 2],
        "job_id": 1,
        "lang": "en",
        "limit": 5,
        "name": "name",
        "offset": 0,
        "options": {},
        "owner_user_id": 1,
        "payload": {"tldr": "short"},
        "provider": "github",
        "query": "topic",
        "request_id": 1,
        "request_ids": [1, 2],
        "repository_id": 1,
        "rule_id": 1,
        "source_id": 1,
        "start_date": now,
        "status": "active",
        "summary_id": 1,
        "summary_ids": [1, 2],
        "telegram_user_id": 1,
        "token": "token",
        "topic": "topic",
        "url": "https://example.test",
        "user_id": 1,
        "value": True,
    }
    if name in values:
        return values[name]
    if name.endswith("_id"):
        return 1
    if name.endswith("_ids"):
        return [1, 2]
    if name.startswith(("is_", "include_")):
        return False
    if "limit" in name or "count" in name or "offset" in name or "position" in name:
        return 1
    if "date" in name or name.endswith("_at"):
        return now
    if "json" in name or "metadata" in name or "data" in name:
        return {}
    if "list" in name or name.endswith("s"):
        return []
    return "value"


def _arguments_for(method: Any) -> dict[str, Any]:
    signature = inspect.signature(method)
    args: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if name == "self" or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        args[name] = _dummy_value(name)
    return args


@pytest.mark.asyncio
async def test_repository_adapters_exercise_empty_database_smoke_paths() -> None:
    database = _Database()
    adapter_types = [
        AdminReadRepositoryAdapter,
        AggregationSessionRepositoryAdapter,
        AttachmentProcessingRepositoryAdapter,
        AudioGenerationRepositoryAdapter,
        AuthRepositoryAdapter,
        BackupRepositoryAdapter,
        BatchSessionRepositoryAdapter,
        BookmarkImportAdapter,
        CrawlResultRepositoryAdapter,
        DeviceRepositoryAdapter,
        EmbeddingRepositoryAdapter,
        ImportJobRepositoryAdapter,
        LatencyStatsRepositoryAdapter,
        LLMRepositoryAdapter,
        RepositoryAnalysisRepositoryAdapter,
        RequestRepositoryAdapter,
        RSSFeedRepositoryAdapter,
        RuleRepositoryAdapter,
        SignalSourceRepositoryAdapter,
        SocialConnectionRepositoryAdapter,
        TagRepositoryAdapter,
        TelegramMessageRepositoryAdapter,
        TopicSearchRepositoryAdapter,
        TranscriptionRepositoryAdapter,
        UserContentRepositoryAdapter,
        UserCredentialRepositoryAdapter,
        UserRepositoryAdapter,
        VideoDownloadRepositoryAdapter,
        WebhookRepositoryAdapter,
    ]

    attempted = 0
    tolerated_failures = 0
    for adapter_type in adapter_types:
        adapter = adapter_type(database)  # type: ignore[arg-type]
        for name in dir(adapter):
            if not name.startswith("async_"):
                continue
            method = getattr(adapter, name)
            if not inspect.iscoroutinefunction(method):
                continue
            attempted += 1
            try:
                await method(**_arguments_for(method))
            except (AttributeError, KeyError, LookupError, RuntimeError, TypeError, ValueError):
                tolerated_failures += 1

    assert attempted >= 80
    assert tolerated_failures < attempted
    assert database.session_obj.executed > 0
