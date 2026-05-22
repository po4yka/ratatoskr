from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from app.adapters.content.platform_extraction import (
    PlatformExtractorContext,
    PlatformRequestLifecycle,
)
from app.adapters.github.platform_extractor import GitHubPlatformExtractor
from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
from app.config import AppConfig
from app.di.platform_extractors import build_platform_extractor_contributions


def _cfg() -> AppConfig:
    return cast(
        "AppConfig",
        SimpleNamespace(
            runtime=SimpleNamespace(aggregation_meta_extractors_enabled=True),
            twitter=SimpleNamespace(enabled=True),
            github=SimpleNamespace(readme_max_bytes=65536),
            embedding=SimpleNamespace(provider="test"),
            vector_store=SimpleNamespace(
                url="http://qdrant.test",
                api_key=None,
                environment="test",
                user_scope="local",
                collection_version="v1",
                connection_timeout=0.1,
            ),
        ),
    )


def test_builtin_platform_contributions_preserve_route_order() -> None:
    contributions = build_platform_extractor_contributions(_cfg())

    assert [contribution.name for contribution in contributions] == [
        "github",
        "academic",
        "youtube",
        "twitter",
        "meta",
    ]


def test_github_route_is_explicit_and_before_generic_scraper_fallback() -> None:
    contributions = build_platform_extractor_contributions(_cfg())

    matching_names = [
        contribution.name
        for contribution in contributions
        if contribution.predicate("https://github.com/openai/openai-python")
    ]

    assert matching_names == ["github"]
    assert contributions[0].name == "github"


def test_academic_route_is_explicit() -> None:
    contributions = build_platform_extractor_contributions(_cfg())

    matching_names = [
        contribution.name
        for contribution in contributions
        if contribution.predicate("https://arxiv.org/abs/2301.00001")
    ]

    assert matching_names == ["academic"]


def test_youtube_route_is_explicit() -> None:
    contributions = build_platform_extractor_contributions(_cfg())

    matching_names = [
        contribution.name
        for contribution in contributions
        if contribution.predicate("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    ]

    assert matching_names == ["youtube"]


def test_twitter_and_x_routes_are_explicit() -> None:
    contributions = build_platform_extractor_contributions(_cfg())

    x_matching_names = [
        contribution.name
        for contribution in contributions
        if contribution.predicate("https://x.com/user/status/123")
    ]
    twitter_matching_names = [
        contribution.name
        for contribution in contributions
        if contribution.predicate("https://twitter.com/user/status/123")
    ]

    assert x_matching_names == ["twitter"]
    assert twitter_matching_names == ["twitter"]


def test_github_contribution_factory_builds_analyze_use_case_in_di(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeQdrantVectorStore:
        available = True

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(
        "app.infrastructure.embedding.embedding_factory.create_embedding_service",
        lambda _cfg: MagicMock(name="embedding_service"),
    )
    monkeypatch.setattr(
        "app.infrastructure.vector.qdrant_store.QdrantVectorStore",
        FakeQdrantVectorStore,
    )
    monkeypatch.setattr(
        "app.core.embedding_space.resolve_embedding_space_identifier",
        lambda _cfg: "test-space",
    )

    cfg = _cfg()
    db = MagicMock(name="db")
    llm_client = MagicMock(name="llm_client")
    message_persistence = MagicMock(name="message_persistence")
    context = PlatformExtractorContext(
        cfg=cfg,
        db=db,
        scraper=MagicMock(name="scraper"),
        response_formatter=MagicMock(name="response_formatter"),
        audit_func=lambda *args, **kwargs: None,
        sem=lambda: MagicMock(),
        message_persistence=message_persistence,
        lifecycle=MagicMock(spec=PlatformRequestLifecycle),
        quality_llm_client=llm_client,
        schedule_crawl_persistence=lambda *args, **kwargs: None,
    )
    github_contribution = build_platform_extractor_contributions(cfg)[0]

    extractor = github_contribution.factory(context)

    assert isinstance(extractor, GitHubPlatformExtractor)
    assert isinstance(extractor._analyze_use_case, AnalyzeRepositoryUseCase)
    assert extractor._analyze_use_case._db is db
    assert extractor._analyze_use_case._agent._llm is llm_client
    assert extractor._analyze_use_case._embedding_gen._db is db
