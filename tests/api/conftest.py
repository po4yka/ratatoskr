"""API test fixtures: async Database, FastAPI TestClient, factories.

Replaces the legacy DatabaseSessionManager + database_proxy wiring with
the async SQLAlchemy port. Each `db`-using test gets a freshly-truncated
Postgres registered as the runtime cache so FastAPI dependencies pick
it up.
"""

from __future__ import annotations

import importlib
import logging
import os
from enum import Enum
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

# All API tests require optional 'api' extras (fastapi, pyjwt, starlette).
# Skip the entire directory when these are not installed.
pytest.importorskip("jwt", reason="PyJWT not installed (install with: pip install .[api])")
pytest.importorskip("fastapi", reason="FastAPI not installed (install with: pip install .[api])")


class StrEnum(str, Enum):
    """Compatibility shim for StrEnum (Python 3.11+)."""


class _NotRequiredMeta(type):
    def __getitem__(cls, item: Any) -> Any:
        return item


class NotRequired(metaclass=_NotRequiredMeta):
    """Compatibility shim for NotRequired (Python 3.11+)."""


import app.db.runtime_database as _db_runtime
from app.api.dependencies.database import clear_session_manager
from app.config.database import DatabaseConfig
from app.db.base import Base
from app.db.models import Request, Summary, TopicSearchIndex, User

if TYPE_CHECKING:
    from app.db.session import Database

logger = logging.getLogger("test.api")


async def _truncate_all_tables(database: Database) -> None:
    """Async helper: TRUNCATE every model table to reset DB state."""
    from sqlalchemy import text as sql_text

    async with database.session() as lookup:
        existing_rows = await lookup.execute(
            sql_text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        existing_tables = {row[0] for row in existing_rows}

    table_names = [
        t.name for t in reversed(Base.metadata.sorted_tables) if t.name in existing_tables
    ]
    if not table_names:
        return
    quoted = ", ".join(f'"{name}"' for name in table_names)
    async with database.transaction() as cleanup:
        await cleanup.execute(sql_text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db(monkeypatch):
    """Provide a freshly-truncated async Database, registered as the runtime cache.

    Function-scoped + async so the asyncpg pool is bound to the same
    event loop the test runs on (pytest-asyncio in `auto` mode creates
    a fresh loop per test). Skips when TEST_DATABASE_URL is unset so
    unit-only runs do not require Postgres.
    """
    from app.db.session import Database

    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for API tests against Postgres")

    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-at-least-32-chars-long-string")
    monkeypatch.setenv("REDIS_ENABLED", "0")
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("RATATOSKR_DATABASE_NULL_POOL", "1")

    clear_session_manager()

    database = Database(config=DatabaseConfig(dsn=dsn, pool_size=2, max_overflow=2))
    await database.migrate()
    await _truncate_all_tables(database)
    await database.engine.dispose()

    # Register as the runtime cache so FastAPI dependencies (and any
    # internal `get_or_create_runtime_database_from_env()` lookup) use it.
    # Post-#9 (eliminate-module-globals) the cache is a 1-element holder list,
    # not a bare attribute.
    _db_runtime._cached_runtime_db_holder[0] = database

    try:
        yield database
    finally:
        _db_runtime._cached_runtime_db_holder[0] = None
        clear_session_manager()
        await database.dispose()


@pytest.fixture(autouse=True)
def collection_service():
    """Build the collection service with the lazily resolved test repository."""
    from app.api.dependencies.database import get_collection_repository
    from app.api.services.collection_service import CollectionService

    return CollectionService(get_collection_repository)


@pytest.fixture
def client(db):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        from starlette.testclient import TestClient

    import app.api.main

    importlib.reload(app.api.main)
    from app.api.main import app

    # Clear in-memory rate limit state accumulated from previous tests
    try:
        from app.api import middleware as _mw

        _mw._local_rate_limits.clear()
    except Exception:  # pragma: no cover
        pass

    return TestClient(app)


@pytest.fixture
def user_factory(db: Database):
    """Async factory for creating test users.

    Returns a coroutine: tests should `await user_factory(...)`. The legacy
    sync version (`user_factory()` returns User) only worked because the
    Peewee proxy was bound to a sync sqlite connection. With the async
    SQLAlchemy port the factory must run in the test's event loop.
    """
    import random

    async def create_user(
        username: str = "testuser",
        telegram_user_id: int | None = None,
        **kwargs: Any,
    ) -> User:
        if telegram_user_id is None:
            telegram_user_id = random.randint(1, 1_000_000)
        async with db.transaction() as session:
            from sqlalchemy import select

            existing = await session.scalar(
                select(User).where(User.telegram_user_id == telegram_user_id)
            )
            if existing is not None:
                user = existing
            else:
                user = User(telegram_user_id=telegram_user_id, username=username, **kwargs)
                session.add(user)
                await session.flush()
        await db.engine.dispose()
        return user

    return create_user


@pytest.fixture
def summary_factory(db: Database, user_factory):
    """Async factory for creating test summaries with full payloads.

    Returns a coroutine. Default payload includes every field the API
    response models declare.
    """
    import random

    async def create_summary(user: User | None = None, **kwargs: Any) -> Summary:
        if user is None:
            user = await user_factory()

        full_payload: dict[str, Any] = {
            "summary_250": "Short summary",
            "summary_1000": "Long summary",
            "tldr": "TLDR",
            "key_ideas": ["Idea 1", "Idea 2"],
            "topic_tags": ["tag1", "tag2"],
            "entities": {"people": ["Person"], "organizations": ["Org"], "locations": ["Loc"]},
            "estimated_reading_time_min": 5,
            "key_stats": [{"label": "Stat", "value": 10, "unit": "%", "sourceExcerpt": "source"}],
            "answered_questions": ["Q1?"],
            "readability": {"method": "FK", "score": 50.0, "level": "Easy"},
            "seo_keywords": ["keyword"],
            "metadata": {
                "title": "Test Title",
                "domain": "example.com",
                "author": "Author",
                "published_at": "2023-01-01",
            },
            "confidence": 0.9,
            "hallucination_risk": "low",
        }

        if kwargs.get("json_payload"):
            full_payload.update(kwargs["json_payload"])
        kwargs["json_payload"] = full_payload

        params: dict[str, Any] = {
            "lang": "en",
            "is_read": False,
            "version": 1,
        }
        params.update(kwargs)

        async with db.transaction() as session:
            rand_id = random.randint(1, 100_000)
            url = f"http://test{rand_id}.com"
            request = Request(
                user_id=user.telegram_user_id,
                input_url=url,
                normalized_url=url,
                status="completed",
                type="url",
            )
            session.add(request)
            await session.flush()
            summary_kwargs: dict[str, Any] = dict(params)
            summary_kwargs["request_id"] = request.id
            summary = Summary(**summary_kwargs)
            session.add(summary)
            await session.flush()
        await db.engine.dispose()
        return summary

    return create_summary


# ==================== Search test fixtures ====================

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.api.models.responses import PaginationInfo, SearchResult, SearchResultsData
from app.api.routers.auth.tokens import create_access_token
from app.api.services.search_service import SearchService
from app.core.time_utils import UTC


def _build_search_results(
    *,
    query: str,
    results: list[SearchResult] | None = None,
    total: int | None = None,
    limit: int = 10,
    offset: int = 0,
    intent: str = "keyword",
    mode: str = "keyword",
    facets: dict[str, Any] | None = None,
) -> SearchResultsData:
    search_results = results or []
    total_items = len(search_results) if total is None else total
    return SearchResultsData(
        results=search_results,
        pagination=PaginationInfo(
            total=total_items,
            limit=limit,
            offset=offset,
            has_more=(offset + limit) < total_items,
        ),
        query=query,
        intent=intent,
        mode=mode,
        facets=facets or {"domains": [], "tags": [], "languages": []},
    )


@pytest_asyncio.fixture
async def search_user(db: Database):
    """Create a test user for search tests."""
    async with db.transaction() as session:
        user = User(telegram_user_id=987654321, username="search_test_user")
        session.add(user)
        await session.flush()
    await db.engine.dispose()
    return user


@pytest.fixture
def search_token(search_user):
    """Create access token for search user."""
    return create_access_token(search_user.telegram_user_id, client_id="test")


@pytest_asyncio.fixture
async def search_data(db: Database, search_user: User):
    """Create test data for search tests."""
    data = []
    now = datetime.now(UTC)

    payload1 = {
        "summary_250": "This is an article about artificial intelligence and machine learning.",
        "summary_1000": "Long summary about AI",
        "tldr": "AI is transforming technology",
        "key_ideas": ["AI", "Machine Learning"],
        "topic_tags": ["#ai", "#technology"],
        "entities": {"people": [], "organizations": [], "locations": []},
        "estimated_reading_time_min": 5,
        "key_stats": [],
        "answered_questions": [],
        "readability": {"method": "FK", "score": 50.0, "level": "Easy"},
        "seo_keywords": ["ai", "artificial intelligence"],
        "metadata": {
            "title": "Introduction to AI",
            "domain": "example.com",
            "author": "John Doe",
            "published_at": "2023-01-01",
        },
        "confidence": 0.9,
        "hallucination_risk": "low",
    }
    payload2 = {
        "summary_250": "Blockchain technology and cryptocurrency explained.",
        "summary_1000": "Long summary about blockchain",
        "tldr": "Blockchain powers cryptocurrencies",
        "key_ideas": ["Blockchain", "Cryptocurrency"],
        "topic_tags": ["#blockchain", "#crypto"],
        "entities": {"people": [], "organizations": [], "locations": []},
        "estimated_reading_time_min": 7,
        "key_stats": [],
        "answered_questions": [],
        "readability": {"method": "FK", "score": 55.0, "level": "Medium"},
        "seo_keywords": ["blockchain", "crypto"],
        "metadata": {
            "title": "Understanding Blockchain",
            "domain": "example.com",
            "author": "Jane Smith",
            "published_at": "2023-02-01",
        },
        "confidence": 0.85,
        "hallucination_risk": "low",
    }

    async with db.transaction() as session:
        req1 = Request(
            user_id=search_user.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/ai-article",
            normalized_url="https://example.com/ai-article",
            created_at=now - timedelta(days=1),
        )
        session.add(req1)
        await session.flush()
        summary1 = Summary(
            request_id=req1.id,
            lang="en",
            json_payload=payload1,
            is_read=False,
        )
        session.add(summary1)
        session.add(
            TopicSearchIndex(
                request_id=req1.id,
                title="Introduction to AI",
                snippet="This is an article about artificial intelligence",
                source="example.com",
                published_at=(now - timedelta(days=1)).isoformat(),
                body="artificial intelligence machine learning",
                tags="#ai #technology",
            )
        )

        req2 = Request(
            user_id=search_user.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/blockchain-article",
            normalized_url="https://example.com/blockchain-article",
            created_at=now - timedelta(days=2),
        )
        session.add(req2)
        await session.flush()
        summary2 = Summary(
            request_id=req2.id,
            lang="en",
            json_payload=payload2,
            is_read=True,
        )
        session.add(summary2)
        session.add(
            TopicSearchIndex(
                request_id=req2.id,
                title="Understanding Blockchain",
                snippet="Blockchain technology and cryptocurrency explained",
                source="example.com",
                published_at=(now - timedelta(days=2)).isoformat(),
                body="blockchain technology cryptocurrency",
                tags="#blockchain #crypto",
            )
        )
        await session.flush()

    await db.engine.dispose()
    data.append({"request": req1, "summary": summary1})
    data.append({"request": req2, "summary": summary2})

    return data


@pytest.fixture
def mock_search_service_results():
    """Patch the search service with a generic empty-result response."""

    async def _search(**kwargs: Any) -> SearchResultsData:
        resolved_mode = kwargs.get("mode", "keyword")
        if resolved_mode == "auto":
            resolved_mode = "keyword"
        return _build_search_results(
            query=kwargs["q"],
            results=[],
            total=0,
            limit=kwargs["limit"],
            offset=kwargs["offset"],
            intent="keyword",
            mode=resolved_mode,
        )

    with patch.object(SearchService, "search_summaries", AsyncMock(side_effect=_search)) as mock:
        yield mock
