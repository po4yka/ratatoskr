"""Tests for ManageStarListsUseCase — star list writes and mirror upkeep."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.use_cases.manage_star_lists import (
    ManageStarListsUseCase,
    RepositoryNotOwnedError,
    StarListNotFoundError,
)


@pytest.fixture(autouse=True)
def _stub_decrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token ciphertext in these fakes is not a real Fernet blob."""
    monkeypatch.setattr(
        "app.application.use_cases.manage_star_lists.decrypt_token",
        lambda _cipher: "ghp_fake",
    )


@dataclass(frozen=True)
class _List:
    id: str
    name: str
    slug: str
    description: str = ""
    is_private: bool = False


def _gateway(summaries: list[_List]) -> MagicMock:
    gateway = MagicMock()
    gateway.__aenter__ = AsyncMock(return_value=gateway)
    gateway.__aexit__ = AsyncMock(return_value=False)
    gateway.fetch_star_list_summaries = AsyncMock(return_value=summaries)
    gateway.create_star_list = AsyncMock(return_value=summaries[0] if summaries else None)
    gateway.update_star_list = AsyncMock(return_value=summaries[0] if summaries else None)
    gateway.delete_star_list = AsyncMock()
    gateway.set_repository_lists = AsyncMock(return_value=[])
    return gateway


def _repo_port(repository: object | None) -> MagicMock:
    port = MagicMock()
    port.get_owned_repository = AsyncMock(return_value=repository)
    port.set_repository_list_names = AsyncMock()
    return port


def _integration_port(active: bool = True) -> MagicMock:
    from app.application.ports.github_integration import GitHubIntegrationStatus

    record = MagicMock()
    record.status = GitHubIntegrationStatus.ACTIVE if active else GitHubIntegrationStatus.REVOKED
    record.encrypted_token = b"cipher"
    port = MagicMock()
    port.get_by_user_id = AsyncMock(return_value=record)
    return port


def _use_case(
    gateway: MagicMock,
    repo_port: MagicMock,
    integration_port: MagicMock | None = None,
) -> ManageStarListsUseCase:
    return ManageStarListsUseCase(
        gateway_factory=lambda _token: gateway,
        repository_repo=repo_port,
        integration_repo=integration_port or _integration_port(),
    )


@pytest.mark.asyncio
async def test_update_resolves_the_list_by_slug_before_mutating():
    gateway = _gateway([_List("UL_1", "Android", "android"), _List("UL_2", "KMP", "kmp")])
    use_case = _use_case(gateway, _repo_port(None))

    await use_case.update_list(42, slug="kmp", name="Kotlin Multiplatform")

    gateway.update_star_list.assert_awaited_once()
    assert gateway.update_star_list.await_args.args[0] == "UL_2"


@pytest.mark.asyncio
async def test_update_accepts_an_exact_name_as_well_as_a_slug():
    gateway = _gateway([_List("UL_2", "KMP", "kmp")])
    use_case = _use_case(gateway, _repo_port(None))

    await use_case.delete_list(42, slug="KMP")

    gateway.delete_star_list.assert_awaited_once_with("UL_2")


@pytest.mark.asyncio
async def test_unknown_slug_raises_instead_of_mutating():
    gateway = _gateway([_List("UL_1", "Android", "android")])
    use_case = _use_case(gateway, _repo_port(None))

    with pytest.raises(StarListNotFoundError):
        await use_case.delete_list(42, slug="nope")

    gateway.delete_star_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_repository_lists_maps_names_to_ids_and_updates_the_mirror():
    gateway = _gateway([_List("UL_1", "Android", "android"), _List("UL_2", "KMP", "kmp")])
    gateway.set_repository_lists = AsyncMock(return_value=["Android", "KMP"])
    repo_port = _repo_port(MagicMock(owner="square", name="metro"))
    use_case = _use_case(gateway, repo_port)

    applied = await use_case.set_repository_lists(
        repository_id=7,
        user_id=42,
        list_names=["Android", "kmp"],
    )

    assert applied == ["Android", "KMP"]
    assert gateway.set_repository_lists.await_args.kwargs["list_ids"] == ["UL_1", "UL_2"]
    assert gateway.set_repository_lists.await_args.kwargs["owner"] == "square"
    repo_port.set_repository_list_names.assert_awaited_once_with(
        repository_id=7, user_id=42, list_names=["Android", "KMP"]
    )


@pytest.mark.asyncio
async def test_set_repository_lists_rejects_an_unknown_name_before_writing():
    """A typo must not read as 'remove from that list'."""
    gateway = _gateway([_List("UL_1", "Android", "android")])
    repo_port = _repo_port(MagicMock(owner="square", name="metro"))
    use_case = _use_case(gateway, repo_port)

    with pytest.raises(StarListNotFoundError):
        await use_case.set_repository_lists(
            repository_id=7, user_id=42, list_names=["Android", "Typo"]
        )

    gateway.set_repository_lists.assert_not_awaited()
    repo_port.set_repository_list_names.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_membership_is_allowed_and_clears_every_list():
    gateway = _gateway([_List("UL_1", "Android", "android")])
    gateway.set_repository_lists = AsyncMock(return_value=[])
    repo_port = _repo_port(MagicMock(owner="square", name="metro"))
    use_case = _use_case(gateway, repo_port)

    applied = await use_case.set_repository_lists(repository_id=7, user_id=42, list_names=[])

    assert applied == []
    assert gateway.set_repository_lists.await_args.kwargs["list_ids"] == []
    repo_port.set_repository_list_names.assert_awaited_once_with(
        repository_id=7, user_id=42, list_names=[]
    )


@pytest.mark.asyncio
async def test_foreign_repository_is_rejected_before_any_github_call():
    gateway = _gateway([_List("UL_1", "Android", "android")])
    use_case = _use_case(gateway, _repo_port(None))

    with pytest.raises(RepositoryNotOwnedError):
        await use_case.set_repository_lists(repository_id=7, user_id=42, list_names=["Android"])

    gateway.fetch_star_list_summaries.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_integration_is_refused_before_any_github_call():
    from app.application.exceptions.github import GitHubIntegrationRequiredError

    gateway = _gateway([_List("UL_1", "Android", "android")])
    integration_port = MagicMock()
    integration_port.get_by_user_id = AsyncMock(return_value=None)
    use_case = _use_case(gateway, _repo_port(None), integration_port)

    with pytest.raises(GitHubIntegrationRequiredError):
        await use_case.list_lists(42)

    gateway.fetch_star_list_summaries.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_integration_is_refused():
    from app.application.exceptions.github import GitHubIntegrationRequiredError

    gateway = _gateway([_List("UL_1", "Android", "android")])
    use_case = _use_case(gateway, _repo_port(None), _integration_port(active=False))

    with pytest.raises(GitHubIntegrationRequiredError):
        await use_case.create_list(42, name="X")

    gateway.create_star_list.assert_not_awaited()
