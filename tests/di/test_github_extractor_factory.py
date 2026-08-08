"""Tests for the shared GitHub platform-extractor factory.

The API, the bot's /star command and the URL router now all build the extractor
here. They differ in what they already own, and the reuse contract is what keeps
that consolidation from quietly costing a second embedding model or a second
analyze use case -- so it is the contract worth pinning.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.di import platform_extractors


@pytest.fixture
def captured(monkeypatch):
    """Capture the kwargs GitHubPlatformExtractor is constructed with."""
    seen: dict[str, Any] = {}

    class _FakeExtractor:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    import app.adapters.github.platform_extractor as module

    monkeypatch.setattr(module, "GitHubPlatformExtractor", _FakeExtractor)
    return seen


def _context(llm_client: Any = object()) -> Any:
    return SimpleNamespace(
        cfg=SimpleNamespace(github=SimpleNamespace()),
        db=object(),
        quality_llm_client=llm_client,
    )


def test_supplied_analyze_use_case_is_used_verbatim(captured, monkeypatch):
    """A caller that already owns an analyze use case must not get a second one.

    This is what makes the API's migration onto the factory behaviour-preserving:
    the object it hands over is the object the extractor ends up with.
    """
    sentinel = object()

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("factory must not build its own embedding generator")

    monkeypatch.setattr(platform_extractors, "_build_repository_embedding_gen", _explode)

    platform_extractors._build_github_platform_extractor(
        _context(),
        analyze_use_case=sentinel,
    )

    assert captured["analyze_use_case"] is sentinel


def test_supplied_embedding_gen_reaches_the_analyze_use_case(captured, monkeypatch):
    """Without an analyze use case, a supplied generator is still reused."""
    generator = object()

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a supplied embedding generator must not be rebuilt")

    monkeypatch.setattr(platform_extractors, "_build_repository_embedding_gen", _explode)

    platform_extractors._build_github_platform_extractor(
        _context(),
        embedding_gen=generator,
        llm_repository=object(),
    )

    assert captured["analyze_use_case"]._embedding_gen is generator


def test_missing_llm_client_is_refused_loudly(captured):
    """No LLM client means repository analysis cannot work; fail at build time."""
    with pytest.raises(RuntimeError, match="requires an LLM client"):
        platform_extractors._build_github_platform_extractor(_context(llm_client=None))
