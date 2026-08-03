"""Tests for GitHub starred/owned/watched repo mirroring enumeration.

All tests are hermetic (no DB, no network).  GitMirrorRepository and
GitHubAPIClient are stubbed with in-memory fakes so we can assert upsert
calls without Postgres or HTTP.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.github.types import GitHubOwnerDTO, RepositoryDTO, StarredItem
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_repo_dto(
    github_id: int,
    full_name: str,
    size: int = 1024,
) -> RepositoryDTO:
    owner_login, repo_name = full_name.split("/", 1)
    return RepositoryDTO(
        id=github_id,
        name=repo_name,
        full_name=full_name,
        owner=GitHubOwnerDTO(login=owner_login, id=github_id * 10, type="User"),
        html_url=f"https://github.com/{full_name}",
        size=size,
    )


def _make_starred(github_id: int, full_name: str, size: int = 1024) -> StarredItem:
    return StarredItem(
        starred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        repo=_make_repo_dto(github_id, full_name, size),
    )


def _make_mirror(mirror_id: int, clone_url: str, name: str | None = None) -> GitMirror:
    return GitMirror(
        id=mirror_id,
        user_id=1,
        source=GitMirrorSource.GITHUB,
        clone_url=clone_url,
        name=name,
        status=GitMirrorStatus.PENDING,
        consecutive_failures=0,
    )


def _build_db_stub(
    integrations: list,
    repository_rows: list[tuple[int, int]] | None = None,
) -> MagicMock:
    """Return a db stub that serves integrations then optional repository rows.

    repository_rows is a list of (id, github_id) pairs returned by the
    second session.execute() call (Repository FK lookup).
    """
    integration_result = MagicMock()
    integration_result.scalars.return_value.all.return_value = integrations

    if repository_rows is None:
        repository_rows = []

    repo_result_mock = MagicMock()
    repo_result_mock.all.return_value = [
        MagicMock(id=r_id, github_id=gh_id) for r_id, gh_id in repository_rows
    ]

    call_count = 0

    async def _execute(stmt):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return integration_result
        return repo_result_mock

    session_mock = MagicMock()
    session_mock.execute = _execute

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.session = MagicMock(return_value=session_ctx)
    return db


# ---------------------------------------------------------------------------
# 1. Owned repos: one upsert per repo with size_kb populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_owned_repos_upserts_one_row_per_repo() -> None:
    """_enumerate_and_upsert_github_repos upserts one mirror row per owned repo."""
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    owned = [
        _make_repo_dto(2001, "alice/my-lib", size=1024),
        _make_repo_dto(2002, "alice/private-tool", size=512),
    ]

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, *, size_kb=None, repository_id=None):
        upserted.append(
            {
                "user_id": user_id,
                "clone_url": clone_url,
                "name": name,
                "size_kb": size_kb,
                "repository_id": repository_id,
            }
        )
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo = MagicMock()
    fake_mirror_repo.upsert_target = fake_upsert

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return owned

        async def list_starred(self):
            async def _empty():
                return
                yield  # make it an async generator

            return _empty()

        async def list_watched_repos(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = False
    cfg.git_backup.mirror_owned = True
    cfg.git_backup.mirror_watched = False
    cfg.git_backup.mirror_orgs = []

    integration = MagicMock(user_id=42, encrypted_token=b"tok")
    db = _build_db_stub([integration])

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(cfg, db)

    assert total == 2
    clone_urls = {u["clone_url"] for u in upserted}
    assert "https://github.com/alice/my-lib.git" in clone_urls
    assert "https://github.com/alice/private-tool.git" in clone_urls

    by_url = {u["clone_url"]: u for u in upserted}
    assert by_url["https://github.com/alice/my-lib.git"]["size_kb"] == 1024
    assert by_url["https://github.com/alice/private-tool.git"]["size_kb"] == 512
    assert by_url["https://github.com/alice/my-lib.git"]["name"] == "alice/my-lib"


# ---------------------------------------------------------------------------
# 2. Watched repos: upserts with correct size_kb
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_watched_repos_upserts_with_size_kb() -> None:
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    watched = [
        _make_repo_dto(3001, "org/upstream-project", size=8192),
    ]

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, *, size_kb=None, repository_id=None):
        upserted.append({"clone_url": clone_url, "size_kb": size_kb})
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo = MagicMock()
    fake_mirror_repo.upsert_target = fake_upsert

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return []

        async def list_starred(self):
            async def _empty():
                return
                yield  # make it an async generator

            return _empty()

        async def list_watched_repos(self):
            return watched

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = False
    cfg.git_backup.mirror_owned = False
    cfg.git_backup.mirror_watched = True
    cfg.git_backup.mirror_orgs = []

    integration = MagicMock(user_id=7, encrypted_token=b"tok")
    db = _build_db_stub([integration])

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(cfg, db)

    assert total == 1
    assert upserted[0]["clone_url"] == "https://github.com/org/upstream-project.git"
    assert upserted[0]["size_kb"] == 8192


# ---------------------------------------------------------------------------
# 3. De-duplication: same repo in starred and owned produces one upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_deduplicates_across_starred_and_owned() -> None:
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    shared_repo = _make_repo_dto(5001, "alice/shared-repo", size=2048)

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, *, size_kb=None, repository_id=None):
        upserted.append({"clone_url": clone_url})
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo = MagicMock()
    fake_mirror_repo.upsert_target = fake_upsert

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_starred(self):
            # list_starred returns a coroutine that resolves to an AsyncIterator.
            # Return an async generator by using an inner helper.
            async def _gen():
                yield _make_starred(shared_repo.id, shared_repo.full_name, shared_repo.size)

            return _gen()

        async def list_owned_repos(self):
            return [shared_repo]

        async def list_watched_repos(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = True
    cfg.git_backup.mirror_owned = True
    cfg.git_backup.mirror_watched = False
    cfg.git_backup.mirror_orgs = []

    integration = MagicMock(user_id=42, encrypted_token=b"tok")
    db = _build_db_stub([integration])

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(cfg, db)

    # The same repo in starred + owned must produce exactly one upsert.
    assert total == 1
    assert upserted[0]["clone_url"] == "https://github.com/alice/shared-repo.git"


# ---------------------------------------------------------------------------
# 4. repository_id FK linked when Repository row exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_links_repository_id_when_row_exists() -> None:
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    repo = _make_repo_dto(6001, "alice/known-repo", size=4096)

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, *, size_kb=None, repository_id=None):
        upserted.append({"clone_url": clone_url, "repository_id": repository_id})
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo = MagicMock()
    fake_mirror_repo.upsert_target = fake_upsert

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return [repo]

        async def list_starred(self):
            async def _empty():
                return
                yield  # make it an async generator

            return _empty()

        async def list_watched_repos(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = False
    cfg.git_backup.mirror_owned = True
    cfg.git_backup.mirror_watched = False
    cfg.git_backup.mirror_orgs = []

    integration = MagicMock(user_id=42, encrypted_token=b"tok")
    # The Repository table has a row with id=77 matching github_id=6001
    db = _build_db_stub([integration], repository_rows=[(77, 6001)])

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(cfg, db)

    assert total == 1
    assert upserted[0]["repository_id"] == 77


# ---------------------------------------------------------------------------
# 5. Per-user API error skips that user but continues for others
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_skips_user_on_api_error() -> None:
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    good_repo = _make_repo_dto(7001, "bob/good-repo", size=100)

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, *, size_kb=None, repository_id=None):
        upserted.append({"user_id": user_id, "clone_url": clone_url})
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo = MagicMock()
    fake_mirror_repo.upsert_target = fake_upsert

    tokens = {b"bad": "bad_token", b"good": "good_token"}

    class _FakeClient:
        def __init__(self, token: str) -> None:
            self._token = token

        async def list_owned_repos(self):
            if self._token == "bad_token":
                raise RuntimeError("simulated API error")
            return [good_repo]

        async def list_starred(self):
            async def _empty():
                return
                yield  # make it an async generator

            return _empty()

        async def list_watched_repos(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = False
    cfg.git_backup.mirror_owned = True
    cfg.git_backup.mirror_watched = False
    cfg.git_backup.mirror_orgs = []

    integrations = [
        MagicMock(user_id=1, encrypted_token=b"bad"),
        MagicMock(user_id=2, encrypted_token=b"good"),
    ]

    integration_result = MagicMock()
    integration_result.scalars.return_value.all.return_value = integrations

    repo_result_mock = MagicMock()
    repo_result_mock.all.return_value = []

    call_count = 0

    async def _execute(stmt):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return integration_result
        return repo_result_mock

    session_mock = MagicMock()
    session_mock.execute = _execute

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.session = MagicMock(return_value=session_ctx)

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", side_effect=lambda b: tokens[b]),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(cfg, db)

    # Only user 2 contributes.
    assert total == 1
    assert upserted[0]["user_id"] == 2


# ---------------------------------------------------------------------------
# 6. size_kb=0 in GitHub response is stored as None (falsy guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerate_stores_none_when_size_is_zero() -> None:
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    zero_size_repo = _make_repo_dto(8001, "alice/empty-repo", size=0)

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, *, size_kb=None, repository_id=None):
        upserted.append({"size_kb": size_kb})
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo = MagicMock()
    fake_mirror_repo.upsert_target = fake_upsert

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return [zero_size_repo]

        async def list_starred(self):
            async def _empty():
                return
                yield  # make it an async generator

            return _empty()

        async def list_watched_repos(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = False
    cfg.git_backup.mirror_owned = True
    cfg.git_backup.mirror_watched = False
    cfg.git_backup.mirror_orgs = []

    integration = MagicMock(user_id=1, encrypted_token=b"tok")
    db = _build_db_stub([integration])

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        await _enumerate_and_upsert_github_repos(cfg, db)

    # size=0 is falsy so size_kb passed as None to preserve DB semantics.
    assert upserted[0]["size_kb"] is None


# ---------------------------------------------------------------------------
# Releasing mirrors of repositories that are no longer starred
# ---------------------------------------------------------------------------


class _CandidateRow(NamedTuple):
    """Stands in for a SQLAlchemy Row: attribute access plus tuple unpacking."""

    id: int
    clone_url: str


def _release_db(rows: list[tuple[int, str]], statements: list[Any] | None = None) -> MagicMock:
    """Fake db whose session returns *rows* as (mirror_id, clone_url) candidates.

    The fake cannot honour a WHERE clause, so *statements* collects each executed
    statement for tests that need to assert on the query itself.
    """

    class _Session:
        async def execute(self, stmt):
            if statements is not None:
                statements.append(stmt)
            result = MagicMock()
            result.all.return_value = [_CandidateRow(mid, url) for mid, url in rows]
            return result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.session = MagicMock(side_effect=_Session)
    return db


@pytest.mark.asyncio
async def test_release_excludes_only_mirrors_absent_from_this_run() -> None:
    """A candidate still present in the enumeration is kept; the missing one is tombstoned."""
    from app.tasks.git_backup_sync import _release_unstarred_mirrors

    db = _release_db(
        [
            (11, "https://github.com/alice/still-there.git"),
            (12, "https://github.com/alice/unstarred.git"),
        ]
    )
    excluded: list[tuple[int, int, str]] = []

    fake_repo = MagicMock()
    fake_repo.record_excluded = AsyncMock(
        side_effect=lambda mid, uid, reason: excluded.append((mid, uid, reason))
    )

    cfg = MagicMock()
    with patch(
        "app.adapters.git_backup.repository.GitMirrorRepository",
        return_value=fake_repo,
    ):
        released = await _release_unstarred_mirrors(
            cfg,
            db,
            {7: {"https://github.com/alice/still-there.git"}},
        )

    assert released == 1
    assert [mid for mid, _, _ in excluded] == [12]
    assert excluded[0][1] == 7
    assert "no longer starred" in excluded[0][2]


@pytest.mark.asyncio
async def test_release_is_a_noop_without_a_completed_enumeration() -> None:
    """No user enumerated successfully -> nothing is touched."""
    from app.tasks.git_backup_sync import _release_unstarred_mirrors

    db = MagicMock()
    db.session = MagicMock(side_effect=AssertionError("must not query without an enumeration"))

    released = await _release_unstarred_mirrors(MagicMock(), db, {})

    assert released == 0


@pytest.mark.asyncio
async def test_release_query_exempts_pinned_mirrors() -> None:
    """A hand-registered backup must survive a star-then-unstar on GitHub.

    Signals 1 and 2 (a repository_id FK, plus source=starred and is_starred=false)
    both hold for such a row once the star sync promotes it, so the pinned column
    is the only thing keeping the sweep off it.
    """
    from app.tasks.git_backup_sync import _release_unstarred_mirrors

    statements: list[Any] = []
    db = _release_db([], statements)

    with patch(
        "app.adapters.git_backup.repository.GitMirrorRepository",
        return_value=MagicMock(),
    ):
        await _release_unstarred_mirrors(MagicMock(), db, {7: set()})

    assert len(statements) == 1
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "git_mirrors.pinned = false" in compiled.lower()


@pytest.mark.asyncio
async def test_bulk_enumeration_does_not_pin_what_it_enrolls() -> None:
    """Only an explicit user request pins; auto-enrolled rows stay sweepable."""
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    seen_pinned: list[bool] = []

    async def fake_upsert(
        user_id, source, clone_url, name, *, size_kb=None, repository_id=None, pinned=False
    ):
        seen_pinned.append(pinned)
        return _make_mirror(len(seen_pinned), clone_url, name=name)

    fake_mirror_repo = MagicMock()
    fake_mirror_repo.upsert_target = fake_upsert

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return [_make_repo_dto(4001, "alice/tool", size=64)]

        async def list_starred(self):
            async def _empty():
                return
                yield  # make it an async generator

            return _empty()

        async def list_watched_repos(self):
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = False
    cfg.git_backup.mirror_owned = True
    cfg.git_backup.mirror_watched = False
    cfg.git_backup.mirror_orgs = []

    db = _build_db_stub([MagicMock(user_id=7, encrypted_token=b"tok")])

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        await _enumerate_and_upsert_github_repos(cfg, db)

    assert seen_pinned == [False]


# ---------------------------------------------------------------------------
# Organisation repositories
# ---------------------------------------------------------------------------


def _org_client(org_repos: dict[str, list], *, failing: set[str] | None = None):
    """Fake client whose only populated category is /orgs/<org>/repos."""
    failing = failing or set()

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return []

        async def list_starred(self):
            async def _empty():
                return
                yield  # make it an async generator

            return _empty()

        async def list_watched_repos(self):
            return []

        async def list_org_repos(self, org):
            if org in failing:
                raise RuntimeError(f"org {org} unreachable")
            return org_repos.get(org, [])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    return _FakeClient


def _org_cfg(orgs: list[str]):
    cfg = MagicMock()
    cfg.git_backup = MagicMock()
    cfg.git_backup.mirror_starred = False
    cfg.git_backup.mirror_owned = False
    cfg.git_backup.mirror_watched = False
    cfg.git_backup.mirror_orgs = orgs
    return cfg


def _recording_repo(sink: list[dict]):
    async def fake_upsert(
        user_id, source, clone_url, name, *, size_kb=None, repository_id=None, pinned=False
    ):
        sink.append({"clone_url": clone_url, "name": name, "size_kb": size_kb})
        return _make_mirror(len(sink), clone_url, name=name)

    repo = MagicMock()
    repo.upsert_target = fake_upsert
    return repo


@pytest.mark.asyncio
async def test_org_repos_are_enumerated_and_upserted() -> None:
    """affiliation=owner never returns org repos, so this is the only path that finds them."""
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    upserted: list[dict] = []
    client = _org_client({"po4yka-labs": [_make_repo_dto(4001, "po4yka-labs/Cartory", size=2048)]})

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=_recording_repo(upserted),
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", client),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, seen = await _enumerate_and_upsert_github_repos(
            _org_cfg(["po4yka-labs"]), _build_db_stub([MagicMock(user_id=7, encrypted_token=b"t")])
        )

    assert total == 1
    assert upserted[0]["clone_url"] == "https://github.com/po4yka-labs/Cartory.git"
    assert upserted[0]["name"] == "po4yka-labs/Cartory"
    assert upserted[0]["size_kb"] == 2048
    assert seen[7] == {"https://github.com/po4yka-labs/Cartory.git"}


@pytest.mark.asyncio
async def test_an_empty_org_list_queries_no_organisation() -> None:
    """The list is the gate; nothing is enumerated by default."""
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    queried: list[str] = []

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return []

        async def list_starred(self):
            async def _empty():
                return
                yield

            return _empty()

        async def list_watched_repos(self):
            return []

        async def list_org_repos(self, org):
            queried.append(org)
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    with (
        patch("app.adapters.git_backup.repository.GitMirrorRepository", return_value=MagicMock()),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(
            _org_cfg([]), _build_db_stub([MagicMock(user_id=7, encrypted_token=b"t")])
        )

    assert total == 0
    assert queried == []


@pytest.mark.asyncio
async def test_one_failing_org_does_not_lose_the_others() -> None:
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    upserted: list[dict] = []
    client = _org_client(
        {"good-org": [_make_repo_dto(4002, "good-org/keeper")]},
        failing={"broken-org"},
    )

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=_recording_repo(upserted),
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", client),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(
            _org_cfg(["broken-org", "good-org"]),
            _build_db_stub([MagicMock(user_id=7, encrypted_token=b"t")]),
        )

    assert total == 1
    assert upserted[0]["clone_url"] == "https://github.com/good-org/keeper.git"


@pytest.mark.asyncio
async def test_a_failing_org_keeps_the_release_sweep_out_of_the_gap() -> None:
    """An incomplete listing must not read as 'those repositories disappeared'."""
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    upserted: list[dict] = []
    client = _org_client({"good-org": [_make_repo_dto(4003, "good-org/keeper")]}, failing={"gone"})

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=_recording_repo(upserted),
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", client),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        _total, seen = await _enumerate_and_upsert_github_repos(
            _org_cfg(["gone", "good-org"]),
            _build_db_stub([MagicMock(user_id=7, encrypted_token=b"t")]),
        )

    # The user is absent from seen_by_user entirely, which is what makes
    # _release_unstarred_mirrors skip them this run.
    assert 7 not in seen


@pytest.mark.asyncio
async def test_a_repo_in_both_owned_and_org_is_upserted_once() -> None:
    from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

    shared = _make_repo_dto(4004, "po4yka-labs/shared")
    upserted: list[dict] = []

    class _FakeClient:
        def __init__(self, token: str) -> None:
            pass

        async def list_owned_repos(self):
            return [shared]

        async def list_starred(self):
            async def _empty():
                return
                yield

            return _empty()

        async def list_watched_repos(self):
            return []

        async def list_org_repos(self, org):
            return [shared]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = _org_cfg(["po4yka-labs"])
    cfg.git_backup.mirror_owned = True

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=_recording_repo(upserted),
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch("app.security.secret_crypto.decrypt_secret", return_value="ghp_fake"),
    ):
        total, _seen = await _enumerate_and_upsert_github_repos(
            cfg, _build_db_stub([MagicMock(user_id=7, encrypted_token=b"t")])
        )

    assert total == 1
