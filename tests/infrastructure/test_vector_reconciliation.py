"""Vector reconciliation contracts."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import (
    Repository,
    RepositoryEmbedding,
    Request,
    Summary,
    SummaryEmbedding,
    User,
)
from app.db.models.repository import RepoSource
from app.db.session import Database
from app.infrastructure.vector.reconciliation import VectorIndexedEntityStats, VectorIndexReconciler

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class FakeVectorStore:
    available = True

    def __init__(self, *, summaries: set[int], repositories: set[int]) -> None:
        self._summaries = summaries
        self._repositories = repositories

    def count(self) -> int:
        return len(self._summaries) + len(self._repositories)

    def get_indexed_summary_ids(self, *, limit: int | None = None, user_id: int | None = None):
        return set(self._summaries)

    def get_indexed_repository_ids(self, *, limit: int | None = None, user_id: int | None = None):
        return set(self._repositories)


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _FakeDatabase:
    def session(self) -> _SessionContext:
        return _SessionContext()


class _FakeEntityAdapter:
    entity_type = "bookmark"

    def __init__(self) -> None:
        self.calls: list[tuple[bool, int, str]] = []

    async def inspect(
        self,
        session,
        *,
        vector_store,
        vector_store_available: bool,
        scan_limit: int,
        expected_model_version: str,
    ) -> VectorIndexedEntityStats:
        self.calls.append((vector_store_available, scan_limit, expected_model_version))
        return VectorIndexedEntityStats(
            entity_type=self.entity_type,
            expected_ids={1, 2, 3},
            indexed_ids={1, 3},
            missing_embeddings=1,
            stale_embeddings=2,
            pending_embeddings=1,
            stale_model_count=1,
            oldest_unindexed_at=dt.datetime(2026, 5, 22, 12, tzinfo=UTC),
            latest_indexed_at=dt.datetime(2026, 5, 22, 12, 30, tzinfo=UTC),
        )


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
        await session.execute(delete(RepositoryEmbedding))
        await session.execute(delete(Repository))
        await session.execute(delete(SummaryEmbedding))
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))


async def _seed(database: Database) -> dict[str, int]:
    now = dt.datetime(2026, 5, 22, 12, tzinfo=UTC)
    async with database.transaction() as session:
        user = User(telegram_user_id=1001, username="owner")
        session.add(user)
        request_one = Request(type="url", status="completed", user_id=1001, updated_at=now)
        request_two = Request(type="url", status="completed", user_id=1001, updated_at=now)
        session.add_all([request_one, request_two])
        await session.flush()
        summary_one = Summary(
            request_id=request_one.id,
            lang="en",
            json_payload={"summary_250": "one"},
            updated_at=now,
        )
        summary_two = Summary(
            request_id=request_two.id,
            lang="en",
            json_payload={"summary_250": "two"},
            updated_at=now,
        )
        repo_one = Repository(
            github_id=10_001,
            owner="owner",
            name="repo-one",
            full_name="owner/repo-one",
            url="https://github.com/owner/repo-one",
            analysis_json={"purpose": "test", "tech_stack": [], "architecture_summary": ""},
            source=RepoSource.STARRED,
            is_starred=True,
            user_id=1001,
        )
        session.add_all([summary_one, summary_two, repo_one])
        await session.flush()
        session.add_all(
            [
                SummaryEmbedding(
                    summary_id=summary_one.id,
                    model_name="expected-model",
                    model_version="1.0",
                    embedding_blob=b"one",
                    dimensions=3,
                    last_indexed_at=now,
                    index_status="indexed",
                ),
                SummaryEmbedding(
                    summary_id=summary_two.id,
                    model_name="old-model",
                    model_version="0.9",
                    embedding_blob=b"two",
                    dimensions=3,
                    last_indexed_at=now - dt.timedelta(hours=1),
                    index_status="indexed",
                ),
            ]
        )
        return {
            "summary_one": summary_one.id,
            "summary_two": summary_two.id,
            "repo_one": repo_one.id,
        }


@pytest.mark.asyncio
async def test_reconciler_accepts_fake_entity_adapter_without_hardcoded_type() -> None:
    adapter = _FakeEntityAdapter()

    report = await VectorIndexReconciler(
        database=_FakeDatabase(),
        vector_store=FakeVectorStore(summaries=set(), repositories=set()),
        adapters=[adapter],
        scan_limit=25,
    ).inspect(now=dt.datetime(2026, 5, 22, 13, tzinfo=UTC))

    assert adapter.calls == [(True, 25, "1.0")]
    assert report.status == "degraded"
    assert report.expected_summaries == 0
    assert report.expected_repositories == 0
    assert report.indexed_points == 0
    assert report.lag_seconds == 3600
    assert report.latest_indexed_at == dt.datetime(2026, 5, 22, 12, 30, tzinfo=UTC)
    entities = cast("dict[str, Any]", report.details["entities"])
    assert entities["bookmark"] == {
        "expected": 3,
        "indexed": 2,
        "missing_vectors": 1,
        "missing_embeddings": 1,
        "stale_embeddings": 2,
        "pending_embeddings": 1,
        "stale_model_count": 1,
        "oldest_unindexed_at": dt.datetime(2026, 5, 22, 12, tzinfo=UTC),
        "latest_indexed_at": dt.datetime(2026, 5, 22, 12, 30, tzinfo=UTC),
    }


@pytest.mark.asyncio
async def test_reconciliation_detects_missing_and_stale_vectors(database: Database) -> None:
    ids = await _seed(database)
    report = await VectorIndexReconciler(
        database=database,
        vector_store=FakeVectorStore(summaries={ids["summary_one"]}, repositories=set()),
        expected_summary_models={"expected-model"},
        expected_repository_models={"expected-model"},
    ).inspect(now=dt.datetime(2026, 5, 22, 13, tzinfo=UTC))

    assert report.status == "degraded"
    assert report.expected_summaries == 2
    assert report.expected_repositories == 1
    assert report.indexed_points == 1
    assert report.missing_summary_vectors == 1
    assert report.missing_repository_vectors == 1
    assert report.missing_repository_embeddings == 1
    assert report.stale_summary_embeddings == 1
    assert report.stale_embedding_model_count == 1
    assert report.lag_seconds > 0


@pytest.mark.asyncio
async def test_reconciliation_detects_pending_repository_embeddings(database: Database) -> None:
    ids = await _seed(database)
    now = dt.datetime(2026, 5, 22, 12, tzinfo=UTC)
    async with database.transaction() as session:
        session.add(
            RepositoryEmbedding(
                repository_id=ids["repo_one"],
                model_name="expected-model",
                model_version="1.0",
                embedding_blob=b"repo",
                dimensions=3,
                content_hash="hash",
                last_indexed_at=now - dt.timedelta(hours=1),
                index_status="pending",
            )
        )

    report = await VectorIndexReconciler(
        database=database,
        vector_store=FakeVectorStore(summaries={ids["summary_one"]}, repositories=set()),
        expected_summary_models={"expected-model"},
        expected_repository_models={"expected-model"},
    ).inspect(now=dt.datetime(2026, 5, 22, 13, tzinfo=UTC))

    assert report.status == "degraded"
    assert report.missing_repository_embeddings == 0
    assert report.stale_repository_embeddings == 1
    assert report.pending_repository_embeddings == 1
    assert report.to_diagnostics()["stale_repository_embeddings"] == 1
    assert report.to_diagnostics()["pending_repository_embeddings"] == 1


@pytest.mark.asyncio
async def test_reconciliation_degrades_cleanly_when_vector_store_disabled(
    database: Database,
) -> None:
    await _seed(database)

    report = await VectorIndexReconciler(
        database=database,
        vector_store=None,
        expected_summary_models={"expected-model"},
        expected_repository_models={"expected-model"},
    ).inspect()

    assert report.status == "disabled"
    assert report.vector_store_available is False
    assert report.indexed_points is None
    assert report.missing_summary_vectors == 0


@pytest.mark.asyncio
async def test_adapters_inspect_concurrently() -> None:
    """Each adapter runs in its own session and the adapters overlap in time.

    The two adapters rendezvous: adapter B only completes once adapter A has
    started. If inspect ran them sequentially, A would block forever waiting on
    an event B has not had a chance to set, and the test would time out.
    """
    a_started = asyncio.Event()
    b_done = asyncio.Event()

    def _stats(entity_type: str) -> VectorIndexedEntityStats:
        return VectorIndexedEntityStats(
            entity_type=entity_type,
            expected_ids=set(),
            indexed_ids=None,
        )

    class _AdapterA:
        entity_type = "summary"
        session_obj: object | None = None

        async def inspect(self, session, **_kw) -> VectorIndexedEntityStats:
            type(self).session_obj = session
            a_started.set()
            await asyncio.wait_for(b_done.wait(), timeout=1.0)
            return _stats("summary")

    class _AdapterB:
        entity_type = "repository"
        session_obj: object | None = None

        async def inspect(self, session, **_kw) -> VectorIndexedEntityStats:
            type(self).session_obj = session
            await asyncio.wait_for(a_started.wait(), timeout=1.0)
            b_done.set()
            return _stats("repository")

    adapter_a = _AdapterA()
    adapter_b = _AdapterB()
    report = await VectorIndexReconciler(
        database=_FakeDatabase(),
        vector_store=None,
        adapters=[adapter_a, adapter_b],
    ).inspect(now=dt.datetime(2026, 5, 22, 13, tzinfo=UTC))

    assert report.status == "disabled"
    # Distinct sessions prove each adapter got its own connection (the
    # precondition for concurrent inspection).
    assert _AdapterA.session_obj is not _AdapterB.session_obj
