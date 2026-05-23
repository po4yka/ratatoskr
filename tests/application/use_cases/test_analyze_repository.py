"""Tests for AnalyzeRepositoryUseCase."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.ports.repository_analysis import RepositoryAnalysisRecord
from app.application.use_cases.analyze_repository import (
    AnalyzeRepositoryUseCase,
    RepositoryNotFoundError,
    _compute_content_hash,
)
from app.core.repo_analysis_schema import (
    KeyConcept,
    RepoAnalysis,
)

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_analysis(confidence: float = 0.9) -> RepoAnalysis:
    return RepoAnalysis(
        purpose="A test repository for unit tests.",
        tech_stack=["Python", "pytest"],
        architecture_summary="Simple flat layout. Entry point is main.py.",
        key_concepts=[KeyConcept(term="fixture", explanation="pytest fixture")],
        code_patterns=[],
        use_cases=["unit testing"],
        target_audience="Developers writing Python tests.",
        maturity="stable",
        key_dependencies=["pytest"],
        hallucination_risk="low",
        confidence=confidence,
    )


class _FakeRepo:
    """Plain-Python stand-in for Repository, avoids SQLAlchemy instrumentation."""

    def __init__(
        self,
        *,
        id: int = 1,
        user_id: int = 123,
        github_id: int = 99,
        full_name: str = "owner/repo",
        description: str | None = "A test repo",
        topics_json: list | None = None,
        languages_json: dict | None = None,
        analysis_json: dict | None = None,
        content_hash: str | None = None,
        pending_analysis: bool = False,
        readme_excerpt: str | None = None,
        primary_language: str | None = "Python",
        license_spdx: str | None = None,
        default_branch: str | None = "main",
        is_starred: bool = False,
    ) -> None:
        from datetime import UTC, datetime

        self.id = id
        self.user_id = user_id
        self.github_id = github_id
        self.full_name = full_name
        parts = full_name.split("/", maxsplit=1)
        self.owner = parts[0]
        self.name = parts[-1]
        self.url = f"https://github.com/{full_name}"
        self.homepage_url = None
        self.description = description
        self.primary_language = primary_language
        self.languages_json = languages_json if languages_json is not None else {"Python": 10000}
        self.topics_json = topics_json if topics_json is not None else ["testing"]
        self.stars = 0
        self.forks = 0
        self.watchers = 0
        self.default_branch = default_branch
        self.license_spdx = license_spdx
        self.is_archived = False
        self.is_fork = False
        self.is_template = False
        self.pushed_at = None
        self.created_at_github = None
        self.readme_excerpt = readme_excerpt
        self.readme_etag = None
        self.analysis_json = analysis_json
        self.analysis_model = None
        self.analysis_at = None
        self.content_hash = content_hash
        self.source = "manual"
        self.is_starred = is_starred
        self.last_synced_at = None
        self.pending_analysis = pending_analysis
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


def _make_repository(**kwargs) -> RepositoryAnalysisRecord:
    fake = _FakeRepo(**kwargs)
    return RepositoryAnalysisRecord(
        id=fake.id,
        user_id=fake.user_id,
        github_id=fake.github_id,
        full_name=fake.full_name,
        description=fake.description,
        topics_json=fake.topics_json,
        languages_json=fake.languages_json,
        analysis_json=fake.analysis_json,
        content_hash=fake.content_hash,
        pending_analysis=fake.pending_analysis,
        readme_excerpt=fake.readme_excerpt,
        primary_language=fake.primary_language,
        license_spdx=fake.license_spdx,
        default_branch=fake.default_branch,
        is_starred=fake.is_starred,
        source=fake.source,
        created_at=fake.created_at,
    )


class _FakeRepositoryAnalysisRepo:
    def __init__(self, repository: RepositoryAnalysisRecord | None) -> None:
        self.repository = repository
        self.get_calls: list[int] = []
        self.save_calls: list[tuple[int, dict, str]] = []

    async def get_for_analysis(self, repository_id: int) -> RepositoryAnalysisRecord | None:
        self.get_calls.append(repository_id)
        return self.repository

    async def save_analysis(
        self,
        repository_id: int,
        *,
        analysis_json: dict,
        content_hash: str,
    ) -> RepositoryAnalysisRecord | None:
        self.save_calls.append((repository_id, analysis_json, content_hash))
        if self.repository is None or self.repository.id != repository_id:
            return None
        self.repository.analysis_json = analysis_json
        self.repository.content_hash = content_hash
        self.repository.pending_analysis = False
        return self.repository


def _stub_agent(return_value: RepoAnalysis | None) -> AsyncMock:
    agent = AsyncMock()
    agent.analyze = AsyncMock(return_value=return_value)
    return agent


def _stub_embedding_gen() -> MagicMock:
    gen = MagicMock()
    gen.regenerate = AsyncMock(return_value=MagicMock())
    return gen


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_analysis_runs_llm_and_persists() -> None:
    """Fresh repository: agent is called, analysis_json persisted, embedding refreshed."""
    repo = _make_repository(analysis_json=None, content_hash=None)
    analysis = _make_analysis()
    repository_repo = _FakeRepositoryAnalysisRepo(repo)
    agent = _stub_agent(analysis)
    embedding_gen = _stub_embedding_gen()

    use_case = AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )
    result = await use_case.analyze(1, correlation_id="corr-1")

    assert result.repository_id == 1
    assert result.analysis is analysis
    assert result.cached is False
    assert result.embedding_refreshed is True

    agent.analyze.assert_awaited_once()
    embedding_gen.regenerate.assert_awaited_once()

    # Check that the repository row received the analysis
    assert repo.analysis_json == analysis.model_dump()
    assert repo.content_hash is not None
    assert repo.pending_analysis is False
    assert repository_repo.save_calls == [(1, analysis.model_dump(), repo.content_hash)]


@pytest.mark.asyncio
async def test_unchanged_content_skips_llm() -> None:
    """Content-hash match with existing analysis: no LLM call, no embedding refresh."""
    repo = _make_repository(
        description="A test repo",
        topics_json=["testing"],
        readme_excerpt=None,
    )
    # Pre-compute the exact hash the use case will compute
    computed_hash = _compute_content_hash(repo)
    repo.content_hash = computed_hash
    repo.analysis_json = _make_analysis().model_dump()

    repository_repo = _FakeRepositoryAnalysisRepo(repo)
    agent = _stub_agent(_make_analysis())
    embedding_gen = _stub_embedding_gen()

    use_case = AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )
    result = await use_case.analyze(1, correlation_id="corr-2")

    assert result.cached is True
    assert result.embedding_refreshed is False
    assert result.analysis is not None

    agent.analyze.assert_not_awaited()
    embedding_gen.regenerate.assert_not_awaited()
    assert repository_repo.save_calls == []


@pytest.mark.asyncio
async def test_force_re_analysis_runs_llm_even_if_unchanged() -> None:
    """force=True bypasses cache even when content_hash matches."""
    repo = _make_repository(
        description="A test repo",
        topics_json=["testing"],
        readme_excerpt=None,
    )
    computed_hash = _compute_content_hash(repo)
    repo.content_hash = computed_hash
    repo.analysis_json = _make_analysis().model_dump()

    repository_repo = _FakeRepositoryAnalysisRepo(repo)
    analysis = _make_analysis(confidence=0.7)
    agent = _stub_agent(analysis)
    embedding_gen = _stub_embedding_gen()

    use_case = AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )
    result = await use_case.analyze(1, force=True, correlation_id="corr-3")

    assert result.cached is False
    assert result.embedding_refreshed is True
    agent.analyze.assert_awaited_once()
    embedding_gen.regenerate.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_failure_preserves_existing_analysis() -> None:
    """Agent returns None: existing analysis_json is NOT overwritten."""
    prior_analysis = _make_analysis(confidence=0.8)
    repo = _make_repository(
        analysis_json=prior_analysis.model_dump(),
        content_hash=None,  # force run regardless
    )

    repository_repo = _FakeRepositoryAnalysisRepo(repo)
    agent = _stub_agent(None)  # agent fails
    embedding_gen = _stub_embedding_gen()

    use_case = AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )
    result = await use_case.analyze(1, correlation_id="corr-4")

    assert result.analysis is None
    assert result.cached is False
    assert result.embedding_refreshed is False

    # prior analysis must not have been touched
    assert repo.analysis_json == prior_analysis.model_dump()
    embedding_gen.regenerate.assert_not_awaited()
    assert repository_repo.save_calls == []


@pytest.mark.asyncio
async def test_repository_not_found_raises() -> None:
    """Missing repository raises RepositoryNotFoundError."""
    repository_repo = _FakeRepositoryAnalysisRepo(None)
    agent = _stub_agent(_make_analysis())
    embedding_gen = _stub_embedding_gen()

    use_case = AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )

    with pytest.raises(RepositoryNotFoundError):
        await use_case.analyze(999, correlation_id="corr-5")

    agent.analyze.assert_not_awaited()
    embedding_gen.regenerate.assert_not_awaited()
