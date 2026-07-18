from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import (
    AuditLog,
    Collection,
    CrawlResult,
    ImportJob,
    LLMCall,
    Request,
    Summary,
    Tag,
    User,
)
from app.db.session import Database
from app.infrastructure.persistence.repositories.admin_read_repository import (
    AdminReadRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    await _clear(db)
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(AuditLog))
        await session.execute(delete(LLMCall))
        await session.execute(delete(CrawlResult))
        await session.execute(delete(Summary))
        await session.execute(delete(Collection))
        await session.execute(delete(Tag))
        await session.execute(delete(ImportJob))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _seed_admin_data(database: Database) -> dt.datetime:
    now = dt.datetime.now(UTC)
    today = now - dt.timedelta(hours=2)
    async with database.transaction() as session:
        session.add_all(
            [
                User(telegram_user_id=9001, username="owner", is_owner=True, created_at=today),
                User(telegram_user_id=9002, username="reader", is_owner=False, created_at=now),
            ]
        )
        completed = Request(
            type="url",
            status="completed",
            correlation_id="admin-completed",
            user_id=9001,
            input_url="https://example.com/ok",
            normalized_url="https://example.com/ok",
            dedupe_hash="admin-completed",
            updated_at=now,
            created_at=now,
        )
        failed = Request(
            type="url",
            status="error",
            correlation_id="admin-error",
            user_id=9001,
            input_url="https://example.com/error",
            normalized_url="https://example.com/error",
            dedupe_hash="admin-error",
            error_type="crawl",
            error_message="failed",
            updated_at=now,
            created_at=now,
        )
        pending = Request(
            type="url",
            status="pending",
            correlation_id="admin-pending",
            user_id=9002,
            input_url="https://example.com/pending",
            normalized_url="https://example.com/pending",
            dedupe_hash="admin-pending",
            updated_at=now,
            created_at=now,
        )
        session.add_all([completed, failed, pending])
        await session.flush()
        session.add_all(
            [
                Summary(request_id=completed.id, lang="en", json_payload={"summary_250": "ok"}),
                Tag(user_id=9001, name="Postgres", normalized_name="postgres"),
                Collection(user_id=9001, name="Inbox"),
                ImportJob(
                    user_id=9001,
                    source_format="html",
                    status="processing",
                    updated_at=now,
                ),
                ImportJob(
                    user_id=9001,
                    source_format="html",
                    status="completed",
                    updated_at=now,
                ),
                CrawlResult(
                    request_id=completed.id,
                    endpoint="firecrawl",
                    firecrawl_success=True,
                    latency_ms=100,
                    updated_at=now,
                ),
                CrawlResult(
                    request_id=failed.id,
                    endpoint="firecrawl",
                    firecrawl_success=False,
                    firecrawl_error_code="AUTH",
                    firecrawl_error_message="Authorization=Bearer scraper-secret failed",
                    latency_ms=200,
                    updated_at=now,
                ),
                LLMCall(
                    request_id=completed.id,
                    provider="openrouter",
                    model="model-a",
                    status="ok",
                    latency_ms=1000,
                    tokens_prompt=10,
                    tokens_completion=20,
                    cost_usd=0.01,
                    created_at=now,
                    updated_at=now,
                ),
                LLMCall(
                    request_id=failed.id,
                    provider="openrouter",
                    model="model-a",
                    status="error",
                    error_text="token=llm-secret failed",
                    latency_ms=3000,
                    tokens_prompt=30,
                    tokens_completion=40,
                    cost_usd=0.03,
                    created_at=now,
                    updated_at=now,
                ),
                AuditLog(
                    level="INFO",
                    event="admin.test",
                    details_json={"user_id": 9001, "ok": True},
                    ts=now,
                ),
                AuditLog(
                    level="WARN",
                    event="admin.other",
                    details_json={"user_id": 9002},
                    ts=now - dt.timedelta(days=1),
                ),
            ]
        )
    return today


@pytest.mark.asyncio
async def test_admin_read_repository_reports_users_jobs_and_health(database: Database) -> None:
    today = await _seed_admin_data(database)
    repo = AdminReadRepositoryAdapter(database)

    users = await repo.async_list_users()
    owner = users["users"][0]
    assert users["total_users"] == 2
    assert owner["user_id"] == 9001
    assert owner["summary_count"] == 1
    assert owner["request_count"] == 2
    assert owner["tag_count"] == 1
    assert owner["collection_count"] == 1

    jobs = await repo.async_job_status(today=today)
    assert jobs["pipeline"] == {
        "pending": 1,
        "processing": 0,
        "completed_today": 1,
        "failed_today": 1,
    }
    assert jobs["imports"] == {"active": 1, "completed_today": 1}

    health = await repo.async_content_health()
    assert health["total_summaries"] == 1
    assert health["total_requests"] == 3
    assert health["failed_requests"] == 1
    assert health["failed_by_error_type"] == {"crawl": 1}
    assert health["recent_failures"][0]["error_message"] == "failed"


@pytest.mark.asyncio
async def test_admin_read_repository_reports_metrics_and_audit_log(database: Database) -> None:
    today = await _seed_admin_data(database)
    repo = AdminReadRepositoryAdapter(database)

    metrics = await repo.async_system_metrics(since=today)
    assert metrics["llm_7d"] == {
        "total_calls": 2,
        "avg_latency_ms": 2000.0,
        "total_prompt_tokens": 40,
        "total_completion_tokens": 60,
        "total_cost_usd": 0.04,
        "error_rate": 0.5,
    }
    assert metrics["scraper_7d"]["firecrawl"] == {
        "total": 2,
        "success": 1,
        "success_rate": 0.5,
    }

    audit = await repo.async_audit_log(
        action="admin.test",
        user_id_filter=9001,
        since=today.isoformat(),
        limit=10,
        offset=0,
    )
    assert audit["total"] == 1
    assert audit["logs"][0]["event"] == "admin.test"
    assert audit["logs"][0]["details"] == {"user_id": 9001, "ok": True}


@pytest.mark.asyncio
async def test_admin_read_repository_reports_provider_diagnostics(database: Database) -> None:
    today = await _seed_admin_data(database)

    async with database.session() as session:
        llm_stats = await AdminReadRepositoryAdapter._llm_provider_stats(session, since=today)
        scraper_stats = await AdminReadRepositoryAdapter._scraper_provider_stats(
            session, since=today
        )

    llm_by_provider = {stat["provider"]: stat for stat in llm_stats}
    llm_openrouter = llm_by_provider["openrouter"]
    assert llm_openrouter["last_failure_at"] is not None
    assert llm_openrouter == {
        "provider": "openrouter",
        "status": "degraded",
        "total_count": 2,
        "failure_count": 1,
        "last_error_code": "error",
        "last_error_message": "token=[REDACTED]",
        "last_failure_at": llm_openrouter["last_failure_at"],
    }

    scraper_by_provider = {stat["provider"]: stat for stat in scraper_stats}
    scraper_firecrawl = scraper_by_provider["firecrawl"]
    assert scraper_firecrawl["last_failure_at"] is not None
    assert scraper_firecrawl == {
        "provider": "firecrawl",
        "status": "degraded",
        "total_count": 2,
        "failure_count": 1,
        "last_error_code": "AUTH",
        "last_error_message": "Authorization=[REDACTED]",
        "last_failure_at": scraper_firecrawl["last_failure_at"],
    }
