"""Pytest configuration and shared fixtures.

This module provides common fixtures for all tests.
"""

import os
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pytest


# Python 3.10 compatibility shims (must be before app imports)
class StrEnum(str, Enum):
    """Compatibility shim for StrEnum (Python 3.11+)."""


class _NotRequiredMeta(type):
    def __getitem__(cls, item: Any) -> Any:
        return item


class NotRequired(metaclass=_NotRequiredMeta):
    """Compatibility shim for NotRequired (Python 3.11+)."""


import datetime as dt_module
import enum
import typing
from datetime import timezone

enum.StrEnum = StrEnum  # type: ignore[misc,assignment]
typing.NotRequired = NotRequired  # type: ignore[assignment]
dt_module.UTC = timezone.utc

from app.api.dependencies.database import clear_session_manager
from app.config import (
    AdaptiveTimeoutConfig,
    AnthropicConfig,
    ApiLimitsConfig,
    AppConfig,
    AttachmentConfig,
    AuthConfig,
    BackgroundProcessorConfig,
    CircuitBreakerConfig,
    ContentLimitsConfig,
    DatabaseConfig,
    FirecrawlConfig,
    OllamaConfig,
    OpenAIConfig,
    OpenRouterConfig,
    QdrantConfig,
    RedisConfig,
    RuntimeConfig,
    SocialConfig,
    SyncConfig,
    TelegramConfig,
    TelegramLimitsConfig,
    TwitterConfig,
    WebSearchConfig,
    YouTubeConfig,
    clear_config_cache,
)
from app.config.integrations import BatchAnalysisConfig
from app.prompts.manager import reset_prompt_manager

# Provide sane defaults for integration/API tests that expect these env vars.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-32-characters-long-123456")
# Bot token must be "digits:at-least-30-chars"
os.environ.setdefault("BOT_TOKEN", "123456789:test-token-secret-part-at-least-30-chars")
os.environ.setdefault("ALLOWED_USER_IDS", "123456789,987654321")
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test_api_hash")
os.environ.setdefault("FIRECRAWL_API_KEY", "fc-test-firecrawl-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test_openrouter_key")
# When TEST_DATABASE_URL is provided (Postgres-backed tests), mirror it into
# DATABASE_URL so app.config.load_config(...) -- which mcp_di.build_mcp_runtime
# and other DI paths transitively call -- finds the same DSN. Use a placeholder
# otherwise so unit tests that don't touch the DB still validate.
if os.environ.get("TEST_DATABASE_URL"):
    os.environ.setdefault("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
else:
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
    )


@pytest.fixture(autouse=True)
def fast_qdrant_retries(monkeypatch):
    """Skip Qdrant connect-retry sleeps so bot tests don't pay 6s/test.

    Production Qdrant retries 3 times with 2s/4s backoff. In test environments
    Qdrant is rarely running and the bot tolerates absence (`required=False`).
    Forcing one attempt with zero delay keeps behaviour identical (the store
    still ends up uninitialized) but skips ~6 seconds of `time.sleep`.
    """
    try:
        from app.infrastructure.vector import qdrant_store as qmod
    except ImportError:
        return

    original = qmod.QdrantVectorStore._connect_with_retry

    def fast(self, max_attempts: int = 3, base_delay: float = 2.0) -> None:
        original(self, max_attempts=1, base_delay=0)

    monkeypatch.setattr(qmod.QdrantVectorStore, "_connect_with_retry", fast)


@pytest.fixture(autouse=True)
def manage_config_cache():
    """Clear cached config between tests that mutate environment variables."""
    clear_config_cache()
    reset_prompt_manager()
    yield
    clear_config_cache()
    reset_prompt_manager()


@pytest.fixture(autouse=True)
def manage_api_session_manager(tmp_path, monkeypatch):
    """Keep API DB singletons isolated and point fallback DB paths at writable storage."""
    monkeypatch.setenv("DB_PATH", str(Path(tmp_path) / "api-session.db"))
    clear_session_manager()
    yield
    clear_session_manager()


@pytest.fixture(autouse=True)
def manage_database_proxy():
    """Save and restore database proxy after each test."""
    try:
        from app.db.models import database_proxy
    except ImportError:
        yield
        return

    old_obj = database_proxy.obj
    yield
    if database_proxy.obj is not old_obj:
        database_proxy.initialize(old_obj)


class MockSummaryRepository:
    """Mock summary repository for testing."""

    def __init__(self):
        """Initialize mock repository."""
        self.summaries: dict[int, dict[str, Any]] = {}
        self.next_id = 1

    async def async_upsert_summary(
        self,
        request_id: int,
        lang: str,
        json_payload: dict[str, Any],
        insights_json: dict[str, Any] | None = None,
        is_read: bool = False,
    ) -> int:
        """Mock upsert summary."""
        self.summaries[request_id] = {
            "id": self.next_id,
            "request_id": request_id,
            "lang": lang,
            "json_payload": json_payload,
            "insights_json": insights_json,
            "is_read": is_read,
            "version": 1,
            "created_at": datetime.utcnow(),
        }
        summary_id = self.next_id
        self.next_id += 1
        return summary_id

    async def async_get_summary_by_request(self, request_id: int) -> dict[str, Any] | None:
        """Mock get summary by request."""
        return self.summaries.get(request_id)

    async def async_get_unread_summaries(
        self,
        uid: int | None,
        cid: int | None,
        limit: int = 10,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Mock get unread summaries."""
        unread = [
            summary for summary in self.summaries.values() if not summary.get("is_read", False)
        ]
        if topic:
            topic_lower = topic.casefold()
            unread = [
                summary
                for summary in unread
                if topic_lower in str(summary["json_payload"]).casefold()
            ]
        return unread[:limit]

    async def async_mark_summary_as_read(self, summary_id: int) -> None:
        """Mock mark summary as read."""
        for summary in self.summaries.values():
            if summary.get("id") == summary_id:
                summary["is_read"] = True
                break

    def to_domain_model(self, db_summary: dict[str, Any]) -> Any:
        """Mock conversion to domain model."""
        from app.domain.models.summary import Summary

        return Summary(
            id=db_summary.get("id"),
            request_id=db_summary["request_id"],
            content=db_summary["json_payload"],
            language=db_summary["lang"],
            version=db_summary.get("version", 1),
            is_read=db_summary.get("is_read", False),
            insights=db_summary.get("insights_json"),
            created_at=db_summary.get("created_at", datetime.utcnow()),
        )


@pytest.fixture
def mock_summary_repository():
    """Provide a mock summary repository."""
    return MockSummaryRepository()


def make_test_app_config(
    db_path: str = "/tmp/test.db",
    allowed_user_ids: tuple[int, ...] = (123456789,),
    **overrides: Any,
) -> AppConfig:
    """Create a complete AppConfig for testing with all required fields.

    Args:
        db_path: Path to the test database file.
        allowed_user_ids: Tuple of allowed Telegram user IDs.
        **overrides: Override any nested config (e.g., telegram=TelegramConfig(...)).

    Returns:
        Complete AppConfig instance suitable for testing.
    """
    defaults: dict[str, Any] = {
        "telegram": TelegramConfig(
            api_id=12345,
            api_hash="test_api_hash_placeholder_value___",
            bot_token="123456789:test-token-secret-part-at-least-30-chars",
            allowed_user_ids=allowed_user_ids,
        ),
        "firecrawl": FirecrawlConfig(api_key="fc-test-api-key-placeholder"),
        "openrouter": OpenRouterConfig(
            api_key="sk-or-test-api-key-placeholder",
            model="test/model",
            fallback_models=(),
            http_referer=None,
            x_title=None,
            max_tokens=None,
            top_p=None,
            temperature=0.2,
        ),
        "youtube": YouTubeConfig(),
        "attachment": AttachmentConfig(),
        "runtime": RuntimeConfig(
            db_path=db_path,
            log_level="INFO",
            request_timeout_sec=5,
            preferred_lang="en",
            debug_payloads=False,
        ),
        "telegram_limits": TelegramLimitsConfig(),
        "database": (
            DatabaseConfig(dsn=os.environ["TEST_DATABASE_URL"])
            if os.environ.get("TEST_DATABASE_URL")
            else DatabaseConfig.model_construct(
                dsn="postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder"
            )
        ),
        "content_limits": ContentLimitsConfig(),
        "vector_store": QdrantConfig(),
        "redis": RedisConfig(enabled=False),
        "api_limits": ApiLimitsConfig(),
        "auth": AuthConfig(),
        "sync": SyncConfig(),
        "background": BackgroundProcessorConfig(),
        "openai": OpenAIConfig(),
        "ollama": OllamaConfig(),
        "anthropic": AnthropicConfig(),
        "circuit_breaker": CircuitBreakerConfig(),
        "web_search": WebSearchConfig(),
        "adaptive_timeout": AdaptiveTimeoutConfig(),
        "batch_analysis": BatchAnalysisConfig(),
        "twitter": TwitterConfig(),
        "social": SocialConfig(),
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


import pytest_asyncio
import respx as _respx


@pytest.fixture
def respx_mock():
    """Per-test respx router; any unmocked httpx call raises immediately."""
    with _respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


# ---------------------------------------------------------------------------
# Async Postgres fixtures (T3 foundation)
#
# These are the new async SQLAlchemy fixtures that test files migrated off
# `tests/db_helpers.py` (the legacy Peewee shim) consume. Tests still on the
# shim are unaffected.
#
# Both fixtures skip cleanly if `TEST_DATABASE_URL` is not set so unit tests
# that do not need a database keep running on developer laptops without
# Postgres.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def database():
    """Function-scoped async `Database` against `TEST_DATABASE_URL`.

    Function-scoped (rather than session-scoped) because pytest-asyncio in
    `auto` mode creates a fresh event loop per test, and an asyncpg pool
    bound to a different loop fails with "attached to a different loop".
    Per-test setup is cheap because `migrate()` is idempotent against an
    already-upgraded schema.
    """
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for async Postgres fixtures")

    from app.db.session import Database

    db = Database(config=DatabaseConfig(dsn=dsn, pool_size=2, max_overflow=2))
    await db.migrate()
    try:
        yield db
    finally:
        await db.dispose()


@pytest_asyncio.fixture
async def session(database):
    """Function-scoped `AsyncSession` with a clean slate.

    Truncates every table BEFORE yielding so each test starts from a known
    empty state, regardless of leftover rows from prior pytest invocations
    (or other tests that bypass this fixture). Per-test cleanup happens
    naturally on the next test's setup.
    """
    from sqlalchemy import text as sql_text

    from app.db.base import Base

    async with database.session() as lookup:
        existing_rows = await lookup.execute(
            sql_text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        existing_tables = {row[0] for row in existing_rows}

    table_names = [
        t.name for t in reversed(Base.metadata.sorted_tables) if t.name in existing_tables
    ]
    if table_names:
        quoted = ", ".join(f'"{name}"' for name in table_names)
        async with database.transaction() as cleanup:
            await cleanup.execute(sql_text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))

    sess = database.session_maker()
    try:
        yield sess
        await sess.commit()
    except Exception:
        await sess.rollback()
        raise
    finally:
        await sess.close()
