"""Tests for GitHubPlatformExtractor."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.github.exceptions import GitHubIntegrationRequiredError
from app.adapters.github.github_api_client import ReadmeResult
from app.adapters.github.platform_extractor import GitHubPlatformExtractor, truncate_readme
from app.adapters.github.types import RepositoryDTO
from app.db.models.repository import (
    GitHubAuthMethod,
    GitHubIntegrationStatus,
    UserGitHubIntegration,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo_dto(**overrides: Any) -> RepositoryDTO:
    raw = json.loads((FIXTURES / "repo_fastapi.json").read_text())
    raw.update(overrides)
    return RepositoryDTO.model_validate(raw)


def _make_integration(
    status: GitHubIntegrationStatus = GitHubIntegrationStatus.ACTIVE,
    user_id: int = 42,
) -> UserGitHubIntegration:
    ig = MagicMock(spec=UserGitHubIntegration)
    ig.user_id = user_id
    ig.status = status
    ig.encrypted_token = b"plaintext_token"  # stub; bypassed via client_factory
    ig.auth_method = GitHubAuthMethod.PAT
    return ig


def _make_db(
    integration: UserGitHubIntegration | None,
    repository_id: int = 99,
    cached_readme: tuple[str | None, str | None] = (None, None),
) -> Any:
    """Build a mock Database whose session() yields the integration and the
    cached README row, and whose transaction() yields a session returning
    *repository_id*."""
    db = MagicMock()

    @asynccontextmanager
    async def _session():
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = integration
        # _load_cached_readme reads (readme_etag, readme_excerpt) via .first().
        result.first.return_value = cached_readme
        session.execute = AsyncMock(return_value=result)
        yield session

    @asynccontextmanager
    async def _transaction():
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=repository_id)
        yield session

    db.session = _session
    db.transaction = _transaction
    return db


def _make_github_config(readme_max_bytes: int = 51200) -> Any:
    cfg = MagicMock()
    cfg.readme_max_bytes = readme_max_bytes
    return cfg


def _make_analyze_use_case() -> AsyncMock:
    uc = AsyncMock()
    uc.analyze = AsyncMock(return_value=MagicMock(repository_id=99, cached=False))
    return uc


def _make_request(url: str = "https://github.com/tiangolo/fastapi", user_id: int = 42) -> Any:
    from app.adapters.content.platform_extraction.models import PlatformExtractionRequest

    return PlatformExtractionRequest(
        message=None,
        url_text=url,
        normalized_url=url,
        correlation_id="test-cid-001",
        user_id=user_id,
    )


def _stub_client_factory(
    repo_dto: RepositoryDTO,
    readme: str | None,
    languages: dict,
    *,
    not_modified: bool = False,
    etag: str | None = None,
) -> Any:
    """Return a factory that produces a context-manager-compatible mock client.

    get_readme now returns a ReadmeResult; *not_modified* simulates a 304.
    """

    def factory(token: str) -> Any:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_repo = AsyncMock(return_value=repo_dto)
        client.get_readme = AsyncMock(
            return_value=ReadmeResult(content=readme, etag=etag, not_modified=not_modified)
        )
        client.get_languages = AsyncMock(return_value=languages)
        return client

    return factory


# ---------------------------------------------------------------------------
# supports()
# ---------------------------------------------------------------------------


class TestSupports:
    def _extractor(self) -> GitHubPlatformExtractor:
        return GitHubPlatformExtractor(
            db=MagicMock(),
            github_config=_make_github_config(),
            analyze_use_case=MagicMock(),
        )

    def test_returns_true_for_github_repo_url(self) -> None:
        ext = self._extractor()
        assert ext.supports("https://github.com/tiangolo/fastapi") is True

    def test_returns_false_for_non_github_url(self) -> None:
        ext = self._extractor()
        assert ext.supports("https://example.com/foo/bar") is False

    def test_returns_false_for_github_sub_path(self) -> None:
        ext = self._extractor()
        assert ext.supports("https://github.com/tiangolo/fastapi/issues") is False


# ---------------------------------------------------------------------------
# extract() — integration loading failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExtractIntegrationErrors:
    async def test_raises_when_no_integration(self) -> None:
        db = _make_db(integration=None)
        ext = GitHubPlatformExtractor(
            db=db,
            github_config=_make_github_config(),
            analyze_use_case=_make_analyze_use_case(),
        )
        with pytest.raises(GitHubIntegrationRequiredError):
            await ext.extract(_make_request())

    async def test_raises_when_integration_inactive(self) -> None:
        ig = _make_integration(status=GitHubIntegrationStatus.NEEDS_REAUTH)
        db = _make_db(integration=ig)
        ext = GitHubPlatformExtractor(
            db=db,
            github_config=_make_github_config(),
            analyze_use_case=_make_analyze_use_case(),
        )
        with pytest.raises(GitHubIntegrationRequiredError):
            await ext.extract(_make_request())


# ---------------------------------------------------------------------------
# extract() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExtractHappyPath:
    async def test_inserts_repository_and_calls_analyze(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_dto = _make_repo_dto()
        languages = {"Python": 123456, "Shell": 1234}
        ig = _make_integration()
        db = _make_db(integration=ig, repository_id=99)
        analyze_uc = _make_analyze_use_case()
        factory = _stub_client_factory(repo_dto, "# README content", languages)

        ext = GitHubPlatformExtractor(
            db=db,
            github_config=_make_github_config(),
            analyze_use_case=analyze_uc,
            client_factory=factory,
        )

        # The lazy ``from app.security.token_crypto import decrypt_token`` inside
        # extract() looks the function up on the real module at call time;
        # monkeypatch the attribute there so we get a stub without poking
        # sys.modules (which leaks state into later tests that hold cached
        # bindings to the original module).
        monkeypatch.setattr("app.security.token_crypto.decrypt_token", lambda _ct: "stub_token")

        result = await ext.extract(_make_request())

        analyze_uc.analyze.assert_awaited_once()
        call_kwargs = analyze_uc.analyze.call_args
        assert call_kwargs.args[0] == 99  # repository_id

        assert result.platform == "github"
        assert result.title == "tiangolo/fastapi"
        assert result.metadata["github_id"] == 164513901
        assert result.metadata["full_name"] == "tiangolo/fastapi"
        assert result.metadata["stars"] == 75000
        assert result.metadata["license"] == "MIT"

    async def test_upserts_existing_repository_preserves_analysis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second extraction must not overwrite analysis_json (it's in the excluded set_)."""
        repo_dto = _make_repo_dto()
        languages = {"Python": 100}
        ig = _make_integration()
        # The upsert excludes analysis_json/content_hash from set_ — verified structurally
        db = _make_db(integration=ig, repository_id=77)
        analyze_uc = _make_analyze_use_case()
        factory = _stub_client_factory(repo_dto, "readme text", languages)

        ext = GitHubPlatformExtractor(
            db=db,
            github_config=_make_github_config(),
            analyze_use_case=analyze_uc,
            client_factory=factory,
        )

        monkeypatch.setattr("app.security.token_crypto.decrypt_token", lambda _ct: "stub_token")

        result = await ext.extract(_make_request())

        # Confirm repository_id used in analyze call is the upserted row id
        analyze_uc.analyze.assert_awaited_once_with(
            77,
            force=False,
            correlation_id="test-cid-001",
            chosen_lang="en",
        )

        # The update_set in _upsert_repository must NOT include analysis_json
        import inspect

        from app.adapters.github import platform_extractor as _mod

        src = inspect.getsource(_mod.GitHubPlatformExtractor._upsert_repository)
        assert "analysis_json" not in src, (
            "analysis_json must not appear in update_set — it would overwrite existing analysis"
        )

    async def test_readme_404_results_in_empty_excerpt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_dto = _make_repo_dto()
        languages: dict[str, int] = {}
        ig = _make_integration()
        db = _make_db(integration=ig, repository_id=55)
        analyze_uc = _make_analyze_use_case()
        # get_readme returns None (404)
        factory = _stub_client_factory(repo_dto, None, languages)

        ext = GitHubPlatformExtractor(
            db=db,
            github_config=_make_github_config(),
            analyze_use_case=analyze_uc,
            client_factory=factory,
        )

        monkeypatch.setattr("app.security.token_crypto.decrypt_token", lambda _ct: "stub_token")

        result = await ext.extract(_make_request())

        # content_text should still be formed, just without readme body
        assert "# README" in result.content_text
        # No exception raised

    async def test_readme_304_preserves_cached_excerpt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 304 (not_modified) keeps the stored README excerpt instead of wiping it."""
        repo_dto = _make_repo_dto()
        ig = _make_integration()
        db = _make_db(
            integration=ig,
            repository_id=55,
            cached_readme=("etag-XYZ", "cached readme body XYZ"),
        )
        analyze_uc = _make_analyze_use_case()
        # content is ignored because not_modified=True simulates a 304 response.
        factory = _stub_client_factory(repo_dto, None, {}, not_modified=True, etag="etag-XYZ")

        ext = GitHubPlatformExtractor(
            db=db,
            github_config=_make_github_config(),
            analyze_use_case=analyze_uc,
            client_factory=factory,
        )

        monkeypatch.setattr("app.security.token_crypto.decrypt_token", lambda _ct: "stub_token")

        result = await ext.extract(_make_request())

        # The cached excerpt is reused (not wiped) on a 304.
        assert "cached readme body XYZ" in result.content_text


# ---------------------------------------------------------------------------
# truncate_readme
# ---------------------------------------------------------------------------


class TestTruncateReadme:
    def test_returns_none_for_none_input(self) -> None:
        assert truncate_readme(None, 100) is None

    def test_returns_unchanged_when_under_limit(self) -> None:
        text = "hello world"
        assert truncate_readme(text, 1000) == text

    def test_truncates_ascii_at_byte_boundary(self) -> None:
        text = "a" * 200
        result = truncate_readme(text, 100)
        assert result is not None
        assert len(result.encode("utf-8")) <= 100

    def test_truncates_multibyte_utf8_at_char_boundary(self) -> None:
        # Each emoji is 4 bytes in UTF-8; at max_bytes=10 we can fit exactly 2 (8 bytes)
        # A 3-byte cut inside a 4-byte sequence must be walked back to a safe boundary
        text = "😀" * 20  # 80 bytes total
        result = truncate_readme(text, 10)
        assert result is not None
        # Result must decode cleanly (no partial multibyte)
        result.encode("utf-8")  # must not raise
        # Must fit within limit
        assert len(result.encode("utf-8")) <= 10

    def test_no_partial_multibyte_sequences(self) -> None:
        # U+00E9 (é) is 2 bytes; U+4E2D (中) is 3 bytes; U+1F600 (😀) is 4 bytes
        text = "café中文😀" * 50
        for max_bytes in (1, 2, 3, 7, 13, 50, 99, 100):
            result = truncate_readme(text, max_bytes)
            if result:
                # Must round-trip without error
                result.encode("utf-8")
                assert len(result.encode("utf-8")) <= max_bytes
