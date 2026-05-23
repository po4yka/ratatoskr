"""Postgres-backed tests for the repository analysis adapter."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db.models import Repository, User
from app.db.session import Database
from app.infrastructure.persistence.repositories.repository_analysis_repository import (
    RepositoryAnalysisRepositoryAdapter,
)


USER_ID = 951_001


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(Repository).where(Repository.user_id == USER_ID))
        await session.execute(delete(User).where(User.telegram_user_id == USER_ID))


@pytest.mark.asyncio
async def test_repository_analysis_adapter_loads_and_saves_analysis(database: Database) -> None:
    await _clear(database)
    try:
        async with database.transaction() as session:
            session.add(User(telegram_user_id=USER_ID, username="repo-analysis"))
            repository = Repository(
                github_id=42_424_242,
                owner="owner",
                name="repo",
                full_name="owner/repo",
                url="https://github.com/owner/repo",
                description="Repository analysis integration test.",
                primary_language="Python",
                languages_json={"Python": 1000},
                topics_json=["analysis", "postgres"],
                default_branch="main",
                license_spdx="MIT",
                readme_excerpt="README excerpt",
                user_id=USER_ID,
                pending_analysis=True,
            )
            session.add(repository)
            await session.flush()
            repository_id = repository.id

        adapter = RepositoryAnalysisRepositoryAdapter(database)
        record = await adapter.get_for_analysis(repository_id)

        assert record is not None
        assert record.full_name == "owner/repo"
        assert record.pending_analysis is True
        assert record.languages_json == {"Python": 1000}
        assert record.topics_json == ["analysis", "postgres"]

        analysis_json = {"purpose": "Adapter integration test", "confidence": 0.9}
        updated = await adapter.save_analysis(
            repository_id,
            analysis_json=analysis_json,
            content_hash="a" * 64,
        )

        assert updated is not None
        assert updated.analysis_json == analysis_json
        assert updated.content_hash == "a" * 64
        assert updated.pending_analysis is False

        async with database.session() as session:
            row = (
                await session.execute(select(Repository).where(Repository.id == repository_id))
            ).scalar_one()
            assert row.analysis_json == analysis_json
            assert row.analysis_model is None
            assert row.analysis_at is not None
            assert row.content_hash == "a" * 64
            assert row.pending_analysis is False
    finally:
        await _clear(database)
