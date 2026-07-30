"""Tests for AddRepositoryUseCase — the three add modes and their failure policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.use_cases.add_repository import (
    AddRepositoryUseCase,
    RepositoryIngestFailedError,
    StarManagementUnavailableError,
)


@dataclass
class _Repo:
    """Stands in for RepositoryDetailDTO; only the fields the use case reads."""

    id: int = 7
    owner: str = "square"
    name: str = "metro"
    full_name: str = "square/metro"
    description: str | None = "A build tool"
    primary_language: str | None = "Kotlin"
    topics: list[str] = field(default_factory=lambda: ["android"])
    readme_excerpt: str | None = "Builds apps."
    is_starred: bool = False


@dataclass(frozen=True)
class _Descriptor:
    name: str
    description: str = ""


def _ingest(repository_id: int = 7) -> MagicMock:
    port = MagicMock()
    port.ingest = AsyncMock(return_value=repository_id)
    return port


def _repo_port(repository: object | None) -> MagicMock:
    port = MagicMock()
    port.get_owned_repository = AsyncMock(return_value=repository)
    return port


def _star_lists(*, lists: list[_Descriptor] | None = None, applied: list[str] | None = None):
    use_case = MagicMock()
    use_case.star_repository = AsyncMock()
    use_case.list_lists = AsyncMock(return_value=lists if lists is not None else [])
    use_case.set_repository_lists = AsyncMock(return_value=applied or [])
    return use_case


def _suggester(list_names: list[str], source: str = "knn") -> MagicMock:
    use_case = MagicMock()
    use_case.suggest = AsyncMock(return_value=MagicMock(list_names=list_names, source=source))
    return use_case


def _mirrors(mirror_id: int = 55) -> MagicMock:
    port = MagicMock()
    port.enroll = AsyncMock(return_value=mirror_id)
    return port


def _build(**overrides) -> AddRepositoryUseCase:
    kwargs: dict = {
        "ingest": _ingest(),
        "repository_repo": _repo_port(_Repo()),
        "star_lists": _star_lists(),
        "suggester": None,
        "mirrors": _mirrors(),
    }
    kwargs.update(overrides)
    return AddRepositoryUseCase(**kwargs)


@pytest.mark.asyncio
async def test_metadata_mode_neither_stars_nor_mirrors():
    """The historical default must stay a pure index."""
    stars = _star_lists()
    mirrors = _mirrors()
    use_case = _build(star_lists=stars, mirrors=mirrors)

    result = await use_case.add(url="https://github.com/square/metro", user_id=42)

    assert result.mode == "metadata"
    assert result.repository_id == 7
    assert result.is_starred is False
    assert result.mirror_id is None
    assert result.warnings == []
    stars.star_repository.assert_not_awaited()
    mirrors.enroll.assert_not_awaited()


@pytest.mark.asyncio
async def test_track_mode_mirrors_without_starring():
    stars = _star_lists()
    mirrors = _mirrors()
    use_case = _build(star_lists=stars, mirrors=mirrors)

    result = await use_case.add(url="https://github.com/square/metro", user_id=42, mode="track")

    assert result.is_starred is False
    assert result.mirror_id == 55
    stars.star_repository.assert_not_awaited()
    # Pinned, so the unstar reconciliation leaves it alone.
    assert mirrors.enroll.await_args.kwargs["pinned"] is True
    assert mirrors.enroll.await_args.kwargs["repository_id"] == 7


@pytest.mark.asyncio
async def test_star_mode_stars_files_and_mirrors():
    stars = _star_lists(lists=[_Descriptor("Android")], applied=["Android"])
    mirrors = _mirrors()
    use_case = _build(
        star_lists=stars,
        suggester=_suggester(["Android"]),
        mirrors=mirrors,
    )

    result = await use_case.add(url="https://github.com/square/metro", user_id=42, mode="star")

    assert result.is_starred is True
    assert result.lists_applied == ["Android"]
    assert result.list_suggestion_source == "knn"
    assert result.mirror_id == 55
    assert result.warnings == []
    stars.star_repository.assert_awaited_once_with(user_id=42, owner="square", name="metro")


@pytest.mark.asyncio
async def test_explicit_lists_skip_the_suggester():
    suggester = _suggester(["Android"])
    stars = _star_lists(lists=[_Descriptor("Rust")], applied=["Rust"])
    use_case = _build(star_lists=stars, suggester=suggester)

    result = await use_case.add(
        url="https://github.com/square/metro",
        user_id=42,
        mode="star",
        list_names=["Rust"],
    )

    assert result.lists_applied == ["Rust"]
    assert result.list_suggestion_source == "explicit"
    suggester.suggest.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_explicit_empty_list_files_it_nowhere_without_suggesting():
    suggester = _suggester(["Android"])
    stars = _star_lists(lists=[_Descriptor("Android")])
    use_case = _build(star_lists=stars, suggester=suggester)

    result = await use_case.add(
        url="https://github.com/square/metro",
        user_id=42,
        mode="star",
        list_names=[],
    )

    assert result.lists_applied == []
    assert result.list_suggestion_source == "explicit"
    suggester.suggest.assert_not_awaited()
    stars.set_repository_lists.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_list_write_keeps_the_star_and_warns():
    """Unstarring to tidy the report would destroy the half that succeeded."""
    stars = _star_lists(lists=[_Descriptor("Android")])
    stars.set_repository_lists = AsyncMock(side_effect=RuntimeError("missing user scope"))
    use_case = _build(star_lists=stars, suggester=_suggester(["Android"]))

    result = await use_case.add(url="https://github.com/square/metro", user_id=42, mode="star")

    assert result.is_starred is True
    assert result.lists_applied == []
    assert len(result.warnings) == 1
    assert "missing user scope" in result.warnings[0]
    # The mirror step still ran.
    assert result.mirror_id == 55


@pytest.mark.asyncio
async def test_a_failed_star_aborts_because_nothing_is_visible_yet():
    stars = _star_lists()
    stars.star_repository = AsyncMock(side_effect=RuntimeError("github down"))
    mirrors = _mirrors()
    use_case = _build(star_lists=stars, mirrors=mirrors)

    with pytest.raises(RuntimeError, match="github down"):
        await use_case.add(url="https://github.com/square/metro", user_id=42, mode="star")

    mirrors.enroll.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_mirror_enrollment_only_warns():
    mirrors = _mirrors()
    mirrors.enroll = AsyncMock(side_effect=RuntimeError("disk full"))
    use_case = _build(mirrors=mirrors)

    result = await use_case.add(url="https://github.com/square/metro", user_id=42, mode="track")

    assert result.mirror_id is None
    assert "disk full" in result.warnings[0]


@pytest.mark.asyncio
async def test_star_mode_without_star_management_is_refused():
    use_case = _build(star_lists=None)

    with pytest.raises(StarManagementUnavailableError):
        await use_case.add(url="https://github.com/square/metro", user_id=42, mode="star")


@pytest.mark.asyncio
async def test_a_row_missing_after_ingest_is_an_error_not_a_partial_success():
    use_case = _build(repository_repo=_repo_port(None))

    with pytest.raises(RepositoryIngestFailedError):
        await use_case.add(url="https://github.com/square/metro", user_id=42)


@pytest.mark.asyncio
async def test_no_live_lists_means_no_suggestion_call():
    suggester = _suggester(["Android"])
    stars = _star_lists(lists=[])
    use_case = _build(star_lists=stars, suggester=suggester)

    result = await use_case.add(url="https://github.com/square/metro", user_id=42, mode="star")

    assert result.is_starred is True
    assert result.lists_applied == []
    assert result.list_suggestion_source == "none"
    suggester.suggest.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_list_set_warns_but_keeps_the_star():
    stars = _star_lists()
    stars.list_lists = AsyncMock(side_effect=RuntimeError("graphql outage"))
    use_case = _build(star_lists=stars, suggester=_suggester(["Android"]))

    result = await use_case.add(url="https://github.com/square/metro", user_id=42, mode="star")

    assert result.is_starred is True
    assert "graphql outage" in result.warnings[0]


@pytest.mark.asyncio
async def test_the_suggester_receives_the_live_list_names():
    suggester = _suggester(["Android"])
    stars = _star_lists(
        lists=[_Descriptor("Android", "Android work"), _Descriptor("Rust")],
        applied=["Android"],
    )
    use_case = _build(star_lists=stars, suggester=suggester)

    await use_case.add(url="https://github.com/square/metro", user_id=42, mode="star")

    candidates = suggester.suggest.await_args.kwargs["available_lists"]
    assert [c.name for c in candidates] == ["Android", "Rust"]
    assert candidates[0].description == "Android work"
    # And the repository's own signals, so the embedding query is meaningful.
    assert suggester.suggest.await_args.kwargs["topics"] == ["android"]
    assert suggester.suggest.await_args.kwargs["repository_id"] == 7
