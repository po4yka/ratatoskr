"""Tests for GitHub gist mirroring: enumeration, upsert, and destination uniqueness.

All tests are hermetic (no DB, no network). GitMirrorRepository is stubbed with a
simple in-memory dict so we can assert upsert calls without a Postgres connection.
GitHubAPIClient is replaced by a thin async fake that returns pre-cooked GistDTO lists.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.git_backup.mirror_service import GitMirrorService
from app.adapters.github.types import GistDTO
from app.core.git_url_safety import is_github_host
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_gist(gist_id: str, description: str | None = None) -> GistDTO:
    from datetime import datetime, timezone

    return GistDTO(
        id=gist_id,
        git_pull_url=f"https://gist.github.com/{gist_id}.git",
        description=description,
        html_url=f"https://gist.github.com/{gist_id}",
        updated_at=datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
    )


def _make_mirror(
    mirror_id: int,
    clone_url: str,
    source: GitMirrorSource = GitMirrorSource.GITHUB,
    name: str | None = None,
    mirror_path: str | None = None,
) -> GitMirror:
    return GitMirror(
        id=mirror_id,
        user_id=1,
        source=source,
        clone_url=clone_url,
        name=name,
        mirror_path=mirror_path,
        status=GitMirrorStatus.PENDING,
        consecutive_failures=0,
    )


def _make_service(data_path: str = "/data/git-mirrors") -> GitMirrorService:
    cfg = MagicMock()
    cfg.data_path = data_path
    cfg.large_repo_threshold_kb = 512_000
    cfg.auto_skip_failing = True
    cfg.max_consecutive_failures = 5
    cfg.failure_cooldown_hours = 24
    cfg.maintenance_strategy = "none"
    cfg.fetch_lfs = False
    cfg.workers = 1
    cfg.large_repo_max_parallel = 1
    return GitMirrorService(
        config=cfg,
        mirror_repo=MagicMock(),
        db=MagicMock(),
        retry_policy=MagicMock(),
        circuit_breaker=MagicMock(),
        maintenance=None,
        lfs=None,
        git_runner=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# 1. is_github_host accepts gist.github.com but rejects lookalikes
# ---------------------------------------------------------------------------


def test_is_github_host_accepts_gist_github_com() -> None:
    assert is_github_host("https://gist.github.com/abc123.git") is True


def test_is_github_host_rejects_gist_lookalike_suffix() -> None:
    assert is_github_host("https://gist.github.com.evil.com/abc.git") is False


def test_is_github_host_rejects_gist_userinfo_trick() -> None:
    assert is_github_host("https://gist.github.com@evil.com/abc.git") is False


def test_is_github_host_rejects_different_gist_subdomain() -> None:
    # Only exactly "gist.github.com" is allowed, not arbitrary subdomains.
    assert is_github_host("https://notgist.github.com/abc.git") is False


# ---------------------------------------------------------------------------
# 2. Destination uniqueness: github.com repos and gist.github.com gists
#    must land in distinct on-disk paths even when the name segment is equal.
# ---------------------------------------------------------------------------


def test_gist_destination_differs_from_repo_destination() -> None:
    """A gist and a regular repo with the same name must not share a path."""
    svc = _make_service()
    data_path = Path("/data/git-mirrors")

    repo_mirror = _make_mirror(
        1,
        "https://github.com/owner/my-snippet.git",
        name="owner_my-snippet",
    )
    gist_mirror = _make_mirror(
        2,
        "https://gist.github.com/abc123.git",
        name="owner_my-snippet",  # same name, different host
    )

    repo_dest = svc._mirror_destination(data_path, repo_mirror)
    gist_dest = svc._mirror_destination(data_path, gist_mirror)

    assert repo_dest != gist_dest, f"Collision: both repo and gist resolve to {repo_dest}"
    # Repo lands under github/github.com/
    assert "github.com" in str(repo_dest)
    assert "gist.github.com" not in str(repo_dest)
    # Gist lands under github/gist.github.com/
    assert "gist.github.com" in str(gist_dest)


def test_two_gists_with_same_name_but_different_ids_differ() -> None:
    """Two gists can have the same description; the name includes the ID so they differ."""
    svc = _make_service()
    data_path = Path("/data/git-mirrors")

    gist1 = _make_mirror(
        1,
        "https://gist.github.com/aaa111.git",
        name="gist:aaa111",
    )
    gist2 = _make_mirror(
        2,
        "https://gist.github.com/bbb222.git",
        name="gist:bbb222",
    )

    dest1 = svc._mirror_destination(data_path, gist1)
    dest2 = svc._mirror_destination(data_path, gist2)

    assert dest1 != dest2


def test_mirror_path_overrides_derivation() -> None:
    """When mirror_path is already set, _mirror_destination returns it verbatim."""
    svc = _make_service()
    data_path = Path("/data/git-mirrors")

    mirror = _make_mirror(
        1,
        "https://gist.github.com/abc123.git",
        mirror_path="/custom/path/abc123.git",
    )
    dest = svc._mirror_destination(data_path, mirror)
    assert dest == Path("/custom/path/abc123.git")


def test_manual_mirror_lands_under_manual_dir() -> None:
    """MANUAL source mirrors still land under <data_path>/manual/."""
    svc = _make_service()
    data_path = Path("/data/git-mirrors")

    mirror = _make_mirror(
        1,
        "https://example.com/my-repo.git",
        source=GitMirrorSource.MANUAL,
        name="my-repo",
    )
    dest = svc._mirror_destination(data_path, mirror)
    assert str(dest).startswith(str(data_path / "manual"))


# ---------------------------------------------------------------------------
# 3. Gist enumeration upserts one GitMirror row per gist
#    Patches target the source modules since _enumerate_and_upsert_gists
#    uses local imports (from X import Y inside the function body).
# ---------------------------------------------------------------------------


def _build_db_stub(integrations: list) -> MagicMock:
    """Return a db stub whose .session() yields a session executing integrations."""
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = integrations

    session_mock = MagicMock()
    session_mock.execute = AsyncMock(return_value=result_mock)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_mock)
    session_ctx.__aexit__ = AsyncMock(return_value=None)

    db = MagicMock()
    db.session = MagicMock(return_value=session_ctx)
    return db


@pytest.mark.asyncio
async def test_enumerate_gists_upserts_one_row_per_gist() -> None:
    """_enumerate_and_upsert_gists creates a mirror row for each gist."""
    from app.tasks.git_backup_sync import _enumerate_and_upsert_gists

    gists = [
        _make_gist("aaa111", "First gist"),
        _make_gist("bbb222", None),  # no description -> uses gist:<id>
    ]

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, **kwargs):
        upserted.append({"user_id": user_id, "clone_url": clone_url, "name": name})
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo_instance = MagicMock()
    fake_mirror_repo_instance.upsert_target = fake_upsert

    fake_client_instance = MagicMock()
    fake_client_instance.list_gists = AsyncMock(return_value=gists)
    fake_client_instance.__aenter__ = AsyncMock(return_value=fake_client_instance)
    fake_client_instance.__aexit__ = AsyncMock(return_value=None)

    cfg = MagicMock()
    cfg.git_backup = MagicMock()

    integration = MagicMock(user_id=42, encrypted_token=b"fake_token")
    db = _build_db_stub([integration])

    # Patch at source module level — where the names are actually resolved at
    # call time inside _enumerate_and_upsert_gists's local import statements.
    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo_instance,
        ),
        patch(
            "app.adapters.github.github_api_client.GitHubAPIClient",
            return_value=fake_client_instance,
        ),
        patch(
            "app.security.secret_crypto.decrypt_secret",
            return_value="ghp_fake_token",
        ),
    ):
        total = await _enumerate_and_upsert_gists(cfg, db)

    assert total == 2
    assert len(upserted) == 2

    assert upserted[0]["clone_url"] == "https://gist.github.com/aaa111.git"
    assert upserted[0]["name"] == "First gist"

    assert upserted[1]["clone_url"] == "https://gist.github.com/bbb222.git"
    assert upserted[1]["name"] == "gist:bbb222"


@pytest.mark.asyncio
async def test_enumerate_gists_skips_user_on_api_error() -> None:
    """A GitHub API error for one user must not abort enumeration for other users."""
    from app.tasks.git_backup_sync import _enumerate_and_upsert_gists

    upserted: list[dict] = []

    async def fake_upsert(user_id, source, clone_url, name, **kwargs):
        upserted.append({"user_id": user_id, "clone_url": clone_url})
        return _make_mirror(len(upserted), clone_url, name=name)

    fake_mirror_repo_instance = MagicMock()
    fake_mirror_repo_instance.upsert_target = fake_upsert

    tokens_by_bytes: dict[bytes, str] = {b"bad": "bad_token", b"good": "good_token"}

    class _FakeClient:
        def __init__(self, token: str) -> None:
            self._token = token

        async def list_gists(self):
            if self._token == "bad_token":
                raise RuntimeError("simulated API error")
            return [_make_gist("ccc333", "Good gist")]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    cfg = MagicMock()
    cfg.git_backup = MagicMock()

    integrations = [
        MagicMock(user_id=1, encrypted_token=b"bad"),
        MagicMock(user_id=2, encrypted_token=b"good"),
    ]
    db = _build_db_stub(integrations)

    with (
        patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            return_value=fake_mirror_repo_instance,
        ),
        patch("app.adapters.github.github_api_client.GitHubAPIClient", _FakeClient),
        patch(
            "app.security.secret_crypto.decrypt_secret",
            side_effect=lambda b: tokens_by_bytes[b],
        ),
    ):
        total = await _enumerate_and_upsert_gists(cfg, db)

    # Only the successful user (user_id=2) contributes a gist.
    assert total == 1
    assert len(upserted) == 1
    assert upserted[0]["user_id"] == 2
