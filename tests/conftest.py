"""Pytest configuration and shared fixtures.

This module provides common fixtures for all tests.
"""

import os
import socket
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pytest


# Python 3.10 compatibility shims (must be before app imports).
#
# The typing.NotRequired shim was REMOVED: NotRequired is native on Python 3.11+
# (this repo targets 3.13), and globally rebinding ``typing.NotRequired`` to a stub
# broke pydantic schema generation for langchain-core message TypedDicts
# (``NotRequired[Literal[...]]`` -> PydanticSchemaGenerationError), which fires when
# langgraph is imported under pytest. T5's real-langgraph tests need the native
# ``typing.NotRequired``. The StrEnum / UTC shims are retained unchanged.
class StrEnum(str, Enum):
    """Compatibility shim for StrEnum (Python 3.11+)."""


import datetime as dt_module
import enum
from datetime import timezone

enum.StrEnum = StrEnum  # type: ignore[misc,assignment]
dt_module.UTC = timezone.utc

from app.api.dependencies.database import clear_session_manager
from app.config import (
    AdaptiveTimeoutConfig,
    ApiLimitsConfig,
    AppConfig,
    AttachmentConfig,
    AuthConfig,
    BackgroundProcessorConfig,
    CircuitBreakerConfig,
    ContentLimitsConfig,
    DatabaseConfig,
    FirecrawlConfig,
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
# Model selection has no code default (production sources it from ratatoskr.yaml).
# The autouse `isolate_ratatoskr_yaml` fixture stubs the YAML loader to {} for
# most tests, so these env baselines are what lets Settings build. Tests that
# clear the environment (`patch.dict(..., clear=True)`) and build config must set
# these themselves -- see tests/_config_env.py (MODEL_SELECTION_ENV).
os.environ.setdefault("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
os.environ.setdefault("OPENROUTER_FALLBACK_MODELS", "qwen/qwen3.6-flash,qwen/qwen3.6-plus-04-02")
os.environ.setdefault("OPENROUTER_FLASH_MODEL", "qwen/qwen3.6-flash")
os.environ.setdefault("OPENROUTER_FLASH_FALLBACK_MODELS", "qwen/qwen3.6-plus-04-02")
os.environ.setdefault("OPENROUTER_LONG_CONTEXT_MODEL", "minimax/minimax-m2")
os.environ.setdefault("ATTACHMENT_VISION_MODEL", "qwen/qwen3-vl-32b-instruct")
os.environ.setdefault("ATTACHMENT_VISION_FALLBACK_MODELS", "moonshotai/kimi-k2.5")
# Behavioral tunables have no code default (post-refactor); supply them so any
# test that builds Settings without a real YAML file still gets a valid config.
os.environ.setdefault("OPENROUTER_TEMPERATURE", "0.2")
os.environ.setdefault("OPENROUTER_ENABLE_STATS", "false")
os.environ.setdefault("OPENROUTER_ENABLE_STRUCTURED_OUTPUTS", "true")
os.environ.setdefault("OPENROUTER_STRUCTURED_OUTPUT_MODE", "json_schema")
os.environ.setdefault("OPENROUTER_REQUIRE_PARAMETERS", "true")
os.environ.setdefault("OPENROUTER_AUTO_FALLBACK_STRUCTURED", "true")
os.environ.setdefault("OPENROUTER_MAX_RESPONSE_SIZE_MB", "10")
os.environ.setdefault("OPENROUTER_ENABLE_PROMPT_CACHING", "true")
os.environ.setdefault("OPENROUTER_PROMPT_CACHE_TTL", "ephemeral")
os.environ.setdefault("OPENROUTER_PROMPT_CACHE_TTL_ANTHROPIC", "1h")
os.environ.setdefault("OPENROUTER_CACHE_SYSTEM_PROMPT", "true")
os.environ.setdefault("OPENROUTER_CACHE_LARGE_CONTENT_THRESHOLD", "4096")
os.environ.setdefault("OPENROUTER_TRANSPORT_RETRY_MAX_ATTEMPTS", "3")
os.environ.setdefault("OPENROUTER_TRANSPORT_RETRY_MIN_WAIT_SEC", "0.5")
os.environ.setdefault("OPENROUTER_TRANSPORT_RETRY_MAX_WAIT_SEC", "5.0")
os.environ.setdefault("ATTACHMENT_PROCESSING_ENABLED", "true")
os.environ.setdefault("ARTICLE_VISION_ENABLED", "true")
os.environ.setdefault("ARTICLE_VISION_MIN_IMAGES", "1")
os.environ.setdefault("VISION_ROUTING_ROLE_FILTER_ENABLED", "true")
os.environ.setdefault("ATTACHMENT_VIDEO_STORAGE_PATH", "/data/video-sources")
os.environ.setdefault("ATTACHMENT_VIDEO_MAX_DOWNLOAD_SIZE_MB", "100")
os.environ.setdefault("ATTACHMENT_VIDEO_TIMEOUT_SEC", "120")
os.environ.setdefault("ATTACHMENT_VIDEO_CLEANUP_AFTER_HOURS", "24")
os.environ.setdefault("ATTACHMENT_VIDEO_FRAME_SAMPLE_COUNT", "4")
os.environ.setdefault("ATTACHMENT_VIDEO_AUDIO_TRANSCRIPTION_ENABLED", "true")
os.environ.setdefault("ATTACHMENT_MAX_IMAGE_SIZE_MB", "10")
os.environ.setdefault("ATTACHMENT_MAX_PDF_SIZE_MB", "20")
os.environ.setdefault("ATTACHMENT_MAX_PDF_PAGES", "50")
os.environ.setdefault("ATTACHMENT_IMAGE_MAX_DIMENSION", "2048")
os.environ.setdefault("ATTACHMENT_STORAGE_PATH", "/data/attachments")
os.environ.setdefault("ATTACHMENT_CLEANUP_AFTER_HOURS", "24")
os.environ.setdefault("ATTACHMENT_MAX_VISION_PAGES", "8")
os.environ.setdefault("ATTACHMENT_PDF_MIN_IMAGE_DIMENSION", "100")
os.environ.setdefault("ATTACHMENT_PDF_MAX_EMBEDDED_IMAGES", "8")
os.environ.setdefault("ATTACHMENT_PDF_MAX_IMAGE_URIS", "12")
os.environ.setdefault("ATTACHMENT_PDF_VECTOR_DRAW_THRESHOLD", "30")
os.environ.setdefault("ATTACHMENT_DOCUMENT_PROCESSING_ENABLED", "true")
os.environ.setdefault("ATTACHMENT_MAX_DOCUMENT_SIZE_MB", "20")
os.environ.setdefault("ATTACHMENT_MAX_DOCUMENT_CHARS", "45000")
os.environ.setdefault("YOUTUBE_DOWNLOAD_ENABLED", "true")
os.environ.setdefault("YOUTUBE_STORAGE_PATH", "/data/videos")
os.environ.setdefault("YOUTUBE_MAX_VIDEO_SIZE_MB", "500")
os.environ.setdefault("YOUTUBE_MAX_STORAGE_GB", "100")
os.environ.setdefault("YOUTUBE_AUTO_CLEANUP_ENABLED", "true")
os.environ.setdefault("YOUTUBE_CLEANUP_AFTER_DAYS", "30")
os.environ.setdefault("YOUTUBE_PREFERRED_QUALITY", "1080p")
os.environ.setdefault("YOUTUBE_SUBTITLE_LANGUAGES", "en,ru")
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


_POSTGRES_FIXTURE_NAMES = frozenset({"database", "db", "session"})
_QUARANTINE_REQUIRED_FIELDS = frozenset({"issue", "owner", "expires"})
_QUARANTINE_RERUNS = 2
_QUARANTINE_RERUN_DELAY_SECONDS = 1


def _apply_quarantine_policy(item: pytest.Item, *, today: date | None = None) -> None:
    """Validate explicit flaky-test quarantine metadata and bound its retries."""
    quarantines = list(item.iter_markers(name="quarantined"))
    flaky_markers = list(item.iter_markers(name="flaky"))

    if flaky_markers:
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.flaky is managed by the test quarantine "
            "policy; use @pytest.mark.quarantined(issue=..., owner=..., "
            "expires='YYYY-MM-DD') instead"
        )
    if not quarantines:
        return
    if len(quarantines) != 1:
        raise pytest.UsageError(
            f"{item.nodeid}: exactly one @pytest.mark.quarantined marker is allowed"
        )

    quarantine = quarantines[0]
    if quarantine.args:
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.quarantined accepts keyword metadata only"
        )

    missing = _QUARANTINE_REQUIRED_FIELDS.difference(quarantine.kwargs)
    if missing:
        fields = ", ".join(sorted(missing))
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.quarantined is missing required metadata: {fields}"
        )

    for field in ("issue", "owner"):
        value = quarantine.kwargs[field]
        if not isinstance(value, str) or not value.strip():
            raise pytest.UsageError(f"{item.nodeid}: quarantine {field} must be a non-empty string")

    expiry = quarantine.kwargs["expires"]
    if not isinstance(expiry, str):
        raise pytest.UsageError(f"{item.nodeid}: quarantine expires must use YYYY-MM-DD")
    try:
        expiry_date = date.fromisoformat(expiry)
    except ValueError as error:
        raise pytest.UsageError(
            f"{item.nodeid}: invalid quarantine expiry {expiry!r}; expected YYYY-MM-DD"
        ) from error
    if expiry_date.isoformat() != expiry:
        raise pytest.UsageError(
            f"{item.nodeid}: invalid quarantine expiry {expiry!r}; expected YYYY-MM-DD"
        )

    current_date = today or datetime.now(timezone.utc).date()
    if expiry_date < current_date:
        raise pytest.UsageError(
            f"{item.nodeid}: quarantine expired on {expiry}; either fix the test or "
            "renew the quarantine with an issue, owner, and future expiry"
        )

    item.add_marker(
        pytest.mark.flaky(
            reruns=_QUARANTINE_RERUNS,
            reruns_delay=_QUARANTINE_RERUN_DELAY_SECONDS,
        )
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply collection policies for PostgreSQL and quarantined tests.

    The marker is attached before pytest evaluates ``-m`` expressions. This
    keeps mixed test modules split correctly: pure unit tests stay in the fast
    job, while tests using the shared ``database``/``session`` fixtures or the
    API ``db`` fixture move to the single Postgres job.

    Tests that open Postgres directly without a shared fixture must declare
    ``@pytest.mark.postgres`` explicitly.
    """
    for item in items:
        _apply_quarantine_policy(item)
        if _POSTGRES_FIXTURE_NAMES.intersection(item.fixturenames):
            item.add_marker(pytest.mark.postgres)


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


# Hosts a `no_network` test may still reach: loopback and the unspecified
# address. Everything else is treated as live network egress.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


class BlockedNetworkError(RuntimeError):
    """Raised when a ``no_network``-marked test attempts live network I/O."""


def _connect_target_is_loopback(address: Any) -> bool:
    # AF_INET/AF_INET6 addresses are ``(host, port[, ...])`` tuples; AF_UNIX
    # addresses are str/bytes paths (never network egress -> always allowed).
    if not isinstance(address, tuple) or not address:
        return True
    return str(address[0]) in _LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def enforce_no_network(request, monkeypatch):
    """Actually block live network I/O for tests marked ``no_network``.

    The marker was purely declarative -- registered in pyproject and applied to
    ~27 suites, but enforced by nothing. A regression that started reaching the
    real network would pass silently: slow, flaky, and a data-egress risk in CI.

    This installs a socket guard for the duration of each marked test: any
    outbound connection to a non-loopback address raises
    :class:`BlockedNetworkError`. Loopback and UNIX-domain sockets stay allowed so
    asyncio internals, local fixtures, and respx's mock transport keep working.
    monkeypatch restores the real socket functions on teardown, so the guard
    never leaks into unmarked tests.
    """
    if request.node.get_closest_marker("no_network") is None:
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def _guard(address: Any, call: str) -> None:
        if not _connect_target_is_loopback(address):
            raise BlockedNetworkError(
                f"{call} to {address!r} blocked: this test is marked "
                "@pytest.mark.no_network and must not perform live network I/O. "
                "Mock the client/transport, or remove the marker if the call is intended."
            )

    def guarded_connect(self, address, *args, **kwargs):
        _guard(address, "socket.connect")
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        _guard(address, "socket.connect_ex")
        return real_connect_ex(self, address, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        _guard(address, "socket.create_connection")
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)


@pytest.fixture(autouse=True)
def isolate_ratatoskr_yaml(request, monkeypatch):
    """Stub out repo-local `config/ratatoskr.yaml` for all tests.

    YAML wins over env per the Settings precedence rule, so without this any
    test that builds `Settings` would inherit dev values (e.g.
    `REDIS_REQUIRED=false`) that contradict its env-var fixture. Stubbing
    `load_ratatoskr_yaml` is more robust than setting `RATATOSKR_CONFIG`
    because many tests use `unittest.mock.patch.dict(os.environ, ..., clear=True)`
    which would erase the env var. Tests that exercise real YAML behaviour
    opt out with `@pytest.mark.uses_real_yaml`.
    """
    if request.node.get_closest_marker("uses_real_yaml") is not None:
        return

    import app.config.config_file as _config_file_module
    import app.config.settings as _settings_module

    def _empty_yaml(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(_config_file_module, "load_ratatoskr_yaml", _empty_yaml)
    if hasattr(_settings_module, "load_ratatoskr_yaml"):
        monkeypatch.setattr(_settings_module, "load_ratatoskr_yaml", _empty_yaml)


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
            # Model selection has no code default; supply the required fields.
            flash_model="test/flash-model",
            flash_fallback_models=(),
            long_context_model="test/long-context-model",
            http_referer=None,
            x_title=None,
            max_tokens=None,
            top_p=None,
            # Behavioral tunables have no code default; supply them explicitly.
            temperature=0.2,
            enable_stats=False,
            enable_structured_outputs=True,
            structured_output_mode="json_schema",
            require_parameters=True,
            auto_fallback_structured=True,
            max_response_size_mb=10,
            enable_prompt_caching=True,
            prompt_cache_ttl="ephemeral",
            prompt_cache_ttl_anthropic="1h",
            cache_system_prompt=True,
            cache_large_content_threshold=4096,
            transport_retry_max_attempts=3,
            transport_retry_min_wait_sec=0.5,
            transport_retry_max_wait_sec=5.0,
        ),
        "youtube": YouTubeConfig(
            # YouTubeConfig behavioral tunables have no code default.
            enabled=True,
            storage_path="/data/videos",
            max_video_size_mb=500,
            max_storage_gb=100,
            auto_cleanup_enabled=True,
            cleanup_after_days=30,
            preferred_quality="1080p",
            subtitle_languages=["en", "ru"],
        ),
        "attachment": AttachmentConfig(
            # Vision-model selection has no code default; supply the required fields.
            vision_model="test/vision-model",
            vision_fallback_models=(),
            # Behavioral tunables have no code default; supply them explicitly.
            enabled=True,
            article_vision_enabled=True,
            article_vision_min_images=1,
            vision_routing_role_filter_enabled=True,
            video_storage_path="/data/video-sources",
            video_max_download_size_mb=100,
            video_timeout_sec=120,
            video_cleanup_after_hours=24,
            video_frame_sample_count=4,
            video_audio_transcription_enabled=True,
            max_image_size_mb=10,
            max_pdf_size_mb=20,
            max_pdf_pages=50,
            image_max_dimension=2048,
            storage_path="/data/attachments",
            cleanup_after_hours=24,
            max_vision_pages_per_pdf=8,
            pdf_min_image_dimension=100,
            pdf_max_embedded_images=8,
            pdf_max_image_uris_total=12,
            pdf_vector_draw_threshold=30,
            document_processing_enabled=True,
            max_document_size_mb=20,
            max_document_chars=45_000,
        ),
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
