"""Hermetic tests for app.tasks.git_backup_sync uncovered branches.

Covers:
- _enumerate_and_upsert_gists: token decrypt failure (continue), GitHub API error (continue),
  successful enumeration path, per-gist upsert exception (swallowed)
- _enumerate_and_upsert_github_repos: token decrypt failure (continue), API error (continue),
  starred/owned/watched per-category paths, per-repo upsert exception (swallowed), dedup
- _index_mirror_readmes: infra unavailable (early return), no candidates (early return),
  mirror with repository_id skipped, mirror without mirror_path skipped,
  mirror_path nonexistent skipped, successful indexing call
- _write_metrics_sync: CSV format (header + row, append without re-header), JSON format,
  parent dir creation
- _export_metrics: CSV path end-to-end
- _prune_stale_excluded: disabled (days<=0), no stale mirrors, on-disk removal, unsafe
  path skipped, Qdrant delete fails (swallowed), DB delete fails (swallowed),
  Qdrant unavailable (available=False) sets store to None
- sync_git_backup task body: disabled guard, redis lock already held, hc_ping start/success,
  hc_ping failure on exception, gist/repo enum flags, index_readmes, reconcile_readmes,
  prune_excluded_days, exit_on_failure raises + fires hc failure, prune exception swallowed

All tests are hermetic: no real DB, no Qdrant, no network, no subprocess calls.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.git_backup import GitBackupConfig

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_git_cfg(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": True,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-test-data",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


def _make_app_config(git_backup: GitBackupConfig, **extra: Any) -> Any:
    cfg = MagicMock()
    cfg.git_backup = git_backup
    cfg.telegram = MagicMock()
    cfg.telegram.api_id = 12345
    cfg.telegram.api_hash = "fake_hash"
    cfg.telegram.bot_token = "fake:token"
    cfg.embedding = MagicMock()
    cfg.vector_store = MagicMock()
    cfg.vector_store.environment = "test"
    cfg.vector_store.user_scope = "owner"
    for k, v in extra.items():
        setattr(cfg, k, v)
    return cfg


def _make_summary(
    *,
    ok: int = 3,
    failed: int = 0,
    skipped: int = 1,
    outcomes: list | None = None,
) -> Any:
    s = MagicMock()
    s.ok = ok
    s.failed = failed
    s.skipped = skipped
    s.total = ok + failed + skipped
    s.outcomes = outcomes if outcomes is not None else []
    return s


class _Ctx:
    """Async context manager yielding a fixed session."""

    def __init__(self, session: Any) -> None:
        self._s = session

    async def __aenter__(self) -> Any:
        return self._s

    async def __aexit__(self, *_: Any) -> bool:
        return False


def _make_fake_db(integrations: list[Any] | None = None) -> MagicMock:
    """Fake Database whose .session() yields a session returning integrations."""
    integrations = integrations or []

    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = integrations
    session.execute = AsyncMock(return_value=result)

    db = MagicMock()
    db.session.return_value = _Ctx(session)
    db.transaction.return_value = _Ctx(session)
    db._session = session
    return db


def _fake_integration(user_id: int = 1, encrypted_token: bytes = b"enc") -> Any:
    integration = MagicMock()
    integration.user_id = user_id
    integration.encrypted_token = encrypted_token
    return integration


# ---------------------------------------------------------------------------
# Lazy-import patch paths
# The functions use `from X import Y` inside their body, so we must patch
# at the source module, not at app.tasks.git_backup_sync.
# ---------------------------------------------------------------------------

_REPO_MOD = "app.adapters.git_backup.repository.GitMirrorRepository"
_GH_CLIENT = "app.adapters.github.github_api_client.GitHubAPIClient"
_DECRYPT = "app.security.secret_crypto.decrypt_secret"
_QDRANT_BUILD = "app.di.shared.build_qdrant_vector_store"
_EMBEDDING_FACTORY = "app.infrastructure.embedding.embedding_factory.create_embedding_service"
_README_INDEXER = "app.infrastructure.search.git_mirror_readme_indexer.GitMirrorReadmeIndexer"
_RECONCILER = "app.infrastructure.search.git_mirror_reconciler.GitMirrorVectorReconciler"
_BUILD_RUNTIME = "app.tasks.deps.build_git_backup_task_runtime"
_PING_START = "app.adapters.git_backup.health_ping.ping_start"
_PING_SUCCESS = "app.adapters.git_backup.health_ping.ping_success"
_PING_FAILURE = "app.adapters.git_backup.health_ping.ping_failure"


# ---------------------------------------------------------------------------
# _enumerate_and_upsert_gists
# ---------------------------------------------------------------------------


class TestEnumerateAndUpsertGists:
    @pytest.mark.asyncio
    async def test_decrypt_failure_is_skipped(self) -> None:
        """Token decryption error logs a warning and continues; returns 0."""
        from app.tasks.git_backup_sync import _enumerate_and_upsert_gists

        integration = _fake_integration(user_id=42)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg())

        with (
            patch(_REPO_MOD),
            patch(_DECRYPT, side_effect=ValueError("bad key")),
        ):
            total = await _enumerate_and_upsert_gists(cfg, db)

        assert total == 0

    @pytest.mark.asyncio
    async def test_gist_api_error_is_skipped(self) -> None:
        """GitHub API error logs a warning and continues; returns 0."""
        from app.tasks.git_backup_sync import _enumerate_and_upsert_gists

        integration = _fake_integration(user_id=7)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg())

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_gists = AsyncMock(side_effect=RuntimeError("API down"))

        with (
            patch(_REPO_MOD),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_gists(cfg, db)

        assert total == 0

    @pytest.mark.asyncio
    async def test_successful_gist_upsert_counts(self) -> None:
        """Successfully upserted gists increment total_upserted."""
        from app.tasks.git_backup_sync import _enumerate_and_upsert_gists

        integration = _fake_integration(user_id=3)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg())

        gist1 = MagicMock()
        gist1.description = "My gist"
        gist1.id = "abc123"
        gist1.git_pull_url = "https://gist.github.com/abc123.git"

        gist2 = MagicMock()
        gist2.description = ""
        gist2.id = "def456"
        gist2.git_pull_url = "https://gist.github.com/def456.git"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_gists = AsyncMock(return_value=[gist1, gist2])

        mock_repo_inst = MagicMock()
        mock_repo_inst.upsert_target = AsyncMock()

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_gists(cfg, db)

        assert total == 2
        calls = mock_repo_inst.upsert_target.call_args_list
        names = [c.kwargs["name"] for c in calls]
        assert "My gist" in names
        assert "gist:def456" in names

    @pytest.mark.asyncio
    async def test_gist_upsert_exception_is_swallowed(self) -> None:
        """Per-gist upsert failure is logged and does not count toward total."""
        from app.tasks.git_backup_sync import _enumerate_and_upsert_gists

        integration = _fake_integration(user_id=5)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg())

        gist = MagicMock()
        gist.description = "failing gist"
        gist.id = "failid"
        gist.git_pull_url = "https://gist.github.com/failid.git"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_gists = AsyncMock(return_value=[gist])

        mock_repo_inst = MagicMock()
        mock_repo_inst.upsert_target = AsyncMock(side_effect=RuntimeError("DB error"))

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_gists(cfg, db)

        assert total == 0

    @pytest.mark.asyncio
    async def test_no_integrations_returns_zero(self) -> None:
        from app.tasks.git_backup_sync import _enumerate_and_upsert_gists

        db = _make_fake_db([])
        cfg = _make_app_config(_make_git_cfg())

        with patch(_REPO_MOD):
            total = await _enumerate_and_upsert_gists(cfg, db)

        assert total == 0


# ---------------------------------------------------------------------------
# _enumerate_and_upsert_github_repos
# ---------------------------------------------------------------------------


class TestEnumerateAndUpsertGithubRepos:
    @pytest.mark.asyncio
    async def test_decrypt_failure_is_skipped(self) -> None:
        from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

        integration = _fake_integration(user_id=10)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg(GIT_BACKUP_MIRROR_STARRED=True))

        with (
            patch(_REPO_MOD),
            patch(_DECRYPT, side_effect=ValueError("bad")),
        ):
            total = await _enumerate_and_upsert_github_repos(cfg, db)

        assert total == 0

    @pytest.mark.asyncio
    async def test_api_error_is_skipped(self) -> None:
        from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

        integration = _fake_integration(user_id=11)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg(GIT_BACKUP_MIRROR_STARRED=True))

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_starred = AsyncMock(side_effect=RuntimeError("rate limited"))

        with (
            patch(_REPO_MOD),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_github_repos(cfg, db)

        assert total == 0

    @pytest.mark.asyncio
    async def test_mirror_starred_fetches_starred(self) -> None:
        from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

        integration = _fake_integration(user_id=20)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg(GIT_BACKUP_MIRROR_STARRED=True))

        repo_dto = MagicMock()
        repo_dto.full_name = "user/repo-a"
        repo_dto.id = 101
        repo_dto.size = 1024

        starred_item = MagicMock()
        starred_item.repo = repo_dto

        # list_starred() is called with `async for item in await client.list_starred()`
        # so the awaitable must return an async iterable.
        async def _async_starred():
            yield starred_item

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_starred = AsyncMock(return_value=_async_starred())

        mock_repo_inst = MagicMock()
        mock_repo_inst.upsert_target = AsyncMock()

        # Second db.session() call for repo_id_by_github_id lookup
        session2 = AsyncMock()
        result2 = MagicMock()
        result2.all.return_value = []
        session2.execute = AsyncMock(return_value=result2)
        db.session.side_effect = [
            _Ctx(db._session),
            _Ctx(session2),
        ]

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_github_repos(cfg, db)

        assert total == 1
        call_kwargs = mock_repo_inst.upsert_target.call_args.kwargs
        assert call_kwargs["name"] == "user/repo-a"
        assert call_kwargs["size_kb"] == 1024

    @pytest.mark.asyncio
    async def test_mirror_owned_fetches_owned_and_size_none(self) -> None:
        """mirror_owned=True fetches owned repos; size=None -> size_kb=None."""
        from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

        integration = _fake_integration(user_id=21)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg(GIT_BACKUP_MIRROR_OWNED=True))

        repo_dto = MagicMock()
        repo_dto.full_name = "user/owned-repo"
        repo_dto.id = 200
        repo_dto.size = None

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_owned_repos = AsyncMock(return_value=[repo_dto])

        mock_repo_inst = MagicMock()
        mock_repo_inst.upsert_target = AsyncMock()

        session2 = AsyncMock()
        result2 = MagicMock()
        result2.all.return_value = []
        session2.execute = AsyncMock(return_value=result2)
        db.session.side_effect = [_Ctx(db._session), _Ctx(session2)]

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_github_repos(cfg, db)

        assert total == 1
        call_kwargs = mock_repo_inst.upsert_target.call_args.kwargs
        assert call_kwargs["size_kb"] is None

    @pytest.mark.asyncio
    async def test_mirror_watched_fetches_watched(self) -> None:
        from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

        integration = _fake_integration(user_id=22)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg(GIT_BACKUP_MIRROR_WATCHED=True))

        repo_dto = MagicMock()
        repo_dto.full_name = "other/watched-repo"
        repo_dto.id = 300
        repo_dto.size = 512

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_watched_repos = AsyncMock(return_value=[repo_dto])

        mock_repo_inst = MagicMock()
        mock_repo_inst.upsert_target = AsyncMock()

        session2 = AsyncMock()
        result2 = MagicMock()
        result2.all.return_value = []
        session2.execute = AsyncMock(return_value=result2)
        db.session.side_effect = [_Ctx(db._session), _Ctx(session2)]

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_github_repos(cfg, db)

        assert total == 1

    @pytest.mark.asyncio
    async def test_repo_upsert_exception_is_swallowed(self) -> None:
        from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

        integration = _fake_integration(user_id=23)
        db = _make_fake_db([integration])
        cfg = _make_app_config(_make_git_cfg(GIT_BACKUP_MIRROR_OWNED=True))

        repo_dto = MagicMock()
        repo_dto.full_name = "user/failing-repo"
        repo_dto.id = 400
        repo_dto.size = 100

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_owned_repos = AsyncMock(return_value=[repo_dto])

        mock_repo_inst = MagicMock()
        mock_repo_inst.upsert_target = AsyncMock(side_effect=RuntimeError("constraint"))

        session2 = AsyncMock()
        result2 = MagicMock()
        result2.all.return_value = []
        session2.execute = AsyncMock(return_value=result2)
        db.session.side_effect = [_Ctx(db._session), _Ctx(session2)]

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_github_repos(cfg, db)

        assert total == 0

    @pytest.mark.asyncio
    async def test_dedup_by_clone_url(self) -> None:
        """Same repo in starred and owned is upserted only once."""
        from app.tasks.git_backup_sync import _enumerate_and_upsert_github_repos

        integration = _fake_integration(user_id=24)
        db = _make_fake_db([integration])
        cfg = _make_app_config(
            _make_git_cfg(GIT_BACKUP_MIRROR_STARRED=True, GIT_BACKUP_MIRROR_OWNED=True)
        )

        shared_dto = MagicMock()
        shared_dto.full_name = "user/shared-repo"
        shared_dto.id = 500
        shared_dto.size = 200

        starred_item = MagicMock()
        starred_item.repo = shared_dto

        async def _async_starred():
            yield starred_item

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_starred = AsyncMock(return_value=_async_starred())
        mock_client.list_owned_repos = AsyncMock(return_value=[shared_dto])

        mock_repo_inst = MagicMock()
        mock_repo_inst.upsert_target = AsyncMock()

        session2 = AsyncMock()
        result2 = MagicMock()
        result2.all.return_value = []
        session2.execute = AsyncMock(return_value=result2)
        db.session.side_effect = [_Ctx(db._session), _Ctx(session2)]

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_DECRYPT, return_value="tok"),
            patch(_GH_CLIENT, return_value=mock_client),
        ):
            total = await _enumerate_and_upsert_github_repos(cfg, db)

        assert total == 1
        assert mock_repo_inst.upsert_target.call_count == 1


# ---------------------------------------------------------------------------
# _index_mirror_readmes
# ---------------------------------------------------------------------------


class TestIndexMirrorReadmes:
    @pytest.mark.asyncio
    async def test_infra_unavailable_returns_early(self) -> None:
        """create_embedding_service raising causes early return without indexing."""
        from app.tasks.git_backup_sync import _index_mirror_readmes

        cfg = _make_app_config(_make_git_cfg())
        db = _make_fake_db()
        summary = _make_summary(outcomes=[])

        with patch(_EMBEDDING_FACTORY, side_effect=RuntimeError("no model")):
            await _index_mirror_readmes(cfg, db, summary)

    @pytest.mark.asyncio
    async def test_no_candidates_returns_early(self) -> None:
        """No successful outcomes -> index_mirrors not called."""
        from app.tasks.git_backup_sync import _index_mirror_readmes

        cfg = _make_app_config(_make_git_cfg())
        db = _make_fake_db()

        failed_outcome = MagicMock()
        failed_outcome.ok = False
        summary = _make_summary(outcomes=[failed_outcome])

        mock_indexer = AsyncMock()

        with (
            patch(_EMBEDDING_FACTORY),
            patch(_QDRANT_BUILD),
            patch(_README_INDEXER, return_value=mock_indexer),
        ):
            await _index_mirror_readmes(cfg, db, summary)

        mock_indexer.index_mirrors.assert_not_called()

    @pytest.mark.asyncio
    async def test_mirror_with_repository_id_is_skipped(self, tmp_path: Path) -> None:
        """repository_id set -> excluded from indexing candidates."""
        from app.tasks.git_backup_sync import _index_mirror_readmes

        cfg = _make_app_config(_make_git_cfg())
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.repository_id = 99
        mirror.mirror_path = str(tmp_path)

        outcome = MagicMock()
        outcome.ok = True
        outcome.mirror = mirror
        summary = _make_summary(outcomes=[outcome])

        mock_indexer = AsyncMock()

        with (
            patch(_EMBEDDING_FACTORY),
            patch(_QDRANT_BUILD),
            patch(_README_INDEXER, return_value=mock_indexer),
        ):
            await _index_mirror_readmes(cfg, db, summary)

        mock_indexer.index_mirrors.assert_not_called()

    @pytest.mark.asyncio
    async def test_mirror_without_mirror_path_is_skipped(self) -> None:
        from app.tasks.git_backup_sync import _index_mirror_readmes

        cfg = _make_app_config(_make_git_cfg())
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.repository_id = None
        mirror.mirror_path = None

        outcome = MagicMock()
        outcome.ok = True
        outcome.mirror = mirror
        summary = _make_summary(outcomes=[outcome])

        mock_indexer = AsyncMock()

        with (
            patch(_EMBEDDING_FACTORY),
            patch(_QDRANT_BUILD),
            patch(_README_INDEXER, return_value=mock_indexer),
        ):
            await _index_mirror_readmes(cfg, db, summary)

        mock_indexer.index_mirrors.assert_not_called()

    @pytest.mark.asyncio
    async def test_mirror_path_nonexistent_is_skipped(self) -> None:
        from app.tasks.git_backup_sync import _index_mirror_readmes

        cfg = _make_app_config(_make_git_cfg())
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.repository_id = None
        mirror.mirror_path = "/definitely/not/here/repo.git"

        outcome = MagicMock()
        outcome.ok = True
        outcome.mirror = mirror
        summary = _make_summary(outcomes=[outcome])

        mock_indexer = AsyncMock()

        with (
            patch(_EMBEDDING_FACTORY),
            patch(_QDRANT_BUILD),
            patch(_README_INDEXER, return_value=mock_indexer),
        ):
            await _index_mirror_readmes(cfg, db, summary)

        mock_indexer.index_mirrors.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_indexing_calls_index_mirrors(self, tmp_path: Path) -> None:
        """Valid candidate -> indexer.index_mirrors called with (mirror, path) pairs."""
        from app.tasks.git_backup_sync import _index_mirror_readmes

        cfg = _make_app_config(_make_git_cfg())
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.repository_id = None
        mirror.mirror_path = str(tmp_path)

        outcome = MagicMock()
        outcome.ok = True
        outcome.mirror = mirror
        summary = _make_summary(outcomes=[outcome])

        mock_indexer = AsyncMock()

        with (
            patch(_EMBEDDING_FACTORY),
            patch(_QDRANT_BUILD),
            patch(_README_INDEXER, return_value=mock_indexer),
        ):
            await _index_mirror_readmes(cfg, db, summary)

        mock_indexer.index_mirrors.assert_called_once()
        candidates_arg = mock_indexer.index_mirrors.call_args[0][0]
        assert len(candidates_arg) == 1
        called_mirror, called_path = candidates_arg[0]
        assert called_mirror is mirror
        assert called_path == tmp_path


# ---------------------------------------------------------------------------
# _write_metrics_sync (CSV path)
# ---------------------------------------------------------------------------


class TestWriteMetricsSync:
    def test_csv_writes_header_then_row(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _write_metrics_sync

        out = tmp_path / "metrics.csv"
        record = {"ok": 3, "failed": 1, "skipped": 0, "total": 4, "duration_seconds": 1.5}

        _write_metrics_sync(out, record, "csv")

        assert out.exists()
        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 1
        assert rows[0]["ok"] == "3"
        assert rows[0]["failed"] == "1"
        assert rows[0]["total"] == "4"

    def test_csv_second_write_appends_without_repeating_header(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _write_metrics_sync

        out = tmp_path / "metrics.csv"
        record = {"ok": 1, "failed": 0, "skipped": 0, "total": 1, "duration_seconds": 0.5}

        _write_metrics_sync(out, record, "csv")
        _write_metrics_sync(out, record, "csv")

        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 2

    def test_json_writes_jsonl(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _write_metrics_sync

        out = tmp_path / "metrics.jsonl"
        record = {"ok": 2, "failed": 0, "skipped": 1, "total": 3, "duration_seconds": 0.1}

        _write_metrics_sync(out, record, "json")

        lines = out.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == record

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _write_metrics_sync

        out = tmp_path / "nested" / "dir" / "metrics.jsonl"
        record = {"ok": 0, "failed": 0, "skipped": 0, "total": 0, "duration_seconds": 0.0}

        _write_metrics_sync(out, record, "json")

        assert out.exists()


# ---------------------------------------------------------------------------
# _export_metrics CSV path end-to-end
# ---------------------------------------------------------------------------


class TestExportMetricsCsvPath:
    @pytest.mark.asyncio
    async def test_csv_export_end_to_end(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _export_metrics

        out = tmp_path / "m.csv"
        git_cfg = _make_git_cfg(
            GIT_BACKUP_METRICS_EXPORT_PATH=str(out),
            GIT_BACKUP_METRICS_FORMAT="csv",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=5, failed=2, skipped=1)

        await _export_metrics(cfg, summary, 7.25)

        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 1
        assert rows[0]["ok"] == "5"
        assert rows[0]["failed"] == "2"


# ---------------------------------------------------------------------------
# _prune_stale_excluded
# ---------------------------------------------------------------------------


class TestPruneStaleExcluded:
    @pytest.mark.asyncio
    async def test_disabled_when_days_zero(self) -> None:
        from app.tasks.git_backup_sync import _prune_stale_excluded

        git_cfg = _make_git_cfg(GIT_BACKUP_PRUNE_EXCLUDED_DAYS=0)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        mock_repo_inst = AsyncMock()
        with patch(_REPO_MOD, return_value=mock_repo_inst):
            await _prune_stale_excluded(cfg, db)

        mock_repo_inst.list_stale_excluded.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_stale_mirrors_exits_early(self) -> None:
        from app.tasks.git_backup_sync import _prune_stale_excluded

        git_cfg = _make_git_cfg(GIT_BACKUP_PRUNE_EXCLUDED_DAYS=7)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        mock_repo_inst = AsyncMock()
        mock_repo_inst.list_stale_excluded = AsyncMock(return_value=[])

        with patch(_REPO_MOD, return_value=mock_repo_inst):
            await _prune_stale_excluded(cfg, db)

        mock_repo_inst.delete_mirror.assert_not_called()

    @pytest.mark.asyncio
    async def test_prunes_disk_and_db_without_qdrant(self, tmp_path: Path) -> None:
        """Prune removes on-disk dir and DB row when Qdrant build raises."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        data_root = tmp_path / "data"
        data_root.mkdir()
        mirror_dir = data_root / "repo.git"
        mirror_dir.mkdir()

        git_cfg = _make_git_cfg(
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=7,
            GIT_BACKUP_DATA_PATH=str(data_root),
        )
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.id = 1
        mirror.mirror_path = str(mirror_dir)

        mock_repo_inst = AsyncMock()
        mock_repo_inst.list_stale_excluded = AsyncMock(return_value=[mirror])
        mock_repo_inst.delete_mirror = AsyncMock()

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_QDRANT_BUILD, side_effect=RuntimeError("no qdrant")),
        ):
            await _prune_stale_excluded(cfg, db)

        mock_repo_inst.delete_mirror.assert_called_once_with(1)
        assert not mirror_dir.exists()

    @pytest.mark.asyncio
    async def test_unsafe_path_is_skipped(self, tmp_path: Path) -> None:
        """mirror_path outside data_root is not removed from disk."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        data_root = tmp_path / "data"
        data_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        git_cfg = _make_git_cfg(
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=1,
            GIT_BACKUP_DATA_PATH=str(data_root),
        )
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.id = 2
        mirror.mirror_path = str(outside)

        mock_repo_inst = AsyncMock()
        mock_repo_inst.list_stale_excluded = AsyncMock(return_value=[mirror])
        mock_repo_inst.delete_mirror = AsyncMock()

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_QDRANT_BUILD, side_effect=RuntimeError("no qdrant")),
        ):
            await _prune_stale_excluded(cfg, db)

        mock_repo_inst.delete_mirror.assert_called_once_with(2)
        assert outside.exists()

    @pytest.mark.asyncio
    async def test_qdrant_delete_failure_is_swallowed(self, tmp_path: Path) -> None:
        """Qdrant delete error logged; disk + DB steps continue."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        data_root = tmp_path / "data"
        data_root.mkdir()
        mirror_dir = data_root / "repo.git"
        mirror_dir.mkdir()

        git_cfg = _make_git_cfg(
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=7,
            GIT_BACKUP_DATA_PATH=str(data_root),
        )
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.id = 3
        mirror.mirror_path = str(mirror_dir)

        mock_repo_inst = AsyncMock()
        mock_repo_inst.list_stale_excluded = AsyncMock(return_value=[mirror])
        mock_repo_inst.delete_mirror = AsyncMock()

        mock_qdrant = MagicMock()
        mock_qdrant.available = True
        mock_qdrant.delete_git_mirror_points = MagicMock(side_effect=RuntimeError("qdrant err"))

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_QDRANT_BUILD, return_value=mock_qdrant),
        ):
            await _prune_stale_excluded(cfg, db)

        mock_repo_inst.delete_mirror.assert_called_once_with(3)

    @pytest.mark.asyncio
    async def test_db_delete_failure_is_swallowed(self, tmp_path: Path) -> None:
        """DB delete error logged; sweep continues without raising."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        data_root = tmp_path / "data"
        data_root.mkdir()

        git_cfg = _make_git_cfg(
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=3,
            GIT_BACKUP_DATA_PATH=str(data_root),
        )
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.id = 4
        mirror.mirror_path = None

        mock_repo_inst = AsyncMock()
        mock_repo_inst.list_stale_excluded = AsyncMock(return_value=[mirror])
        mock_repo_inst.delete_mirror = AsyncMock(side_effect=RuntimeError("FK constraint"))

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_QDRANT_BUILD, side_effect=RuntimeError("no qdrant")),
        ):
            await _prune_stale_excluded(cfg, db)

        mock_repo_inst.delete_mirror.assert_called_once_with(4)

    @pytest.mark.asyncio
    async def test_qdrant_available_false_sets_store_to_none(self, tmp_path: Path) -> None:
        """When qdrant_store.available=False, Qdrant delete steps are skipped."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        data_root = tmp_path / "data"
        data_root.mkdir()

        git_cfg = _make_git_cfg(
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=5,
            GIT_BACKUP_DATA_PATH=str(data_root),
        )
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        mirror = MagicMock()
        mirror.id = 5
        mirror.mirror_path = None

        mock_repo_inst = AsyncMock()
        mock_repo_inst.list_stale_excluded = AsyncMock(return_value=[mirror])
        mock_repo_inst.delete_mirror = AsyncMock()

        mock_qdrant = MagicMock()
        mock_qdrant.available = False

        with (
            patch(_REPO_MOD, return_value=mock_repo_inst),
            patch(_QDRANT_BUILD, return_value=mock_qdrant),
        ):
            await _prune_stale_excluded(cfg, db)

        mock_qdrant.delete_git_mirror_points.assert_not_called()
        mock_repo_inst.delete_mirror.assert_called_once_with(5)


# ---------------------------------------------------------------------------
# sync_git_backup task body
# ---------------------------------------------------------------------------


def _make_lock_ctx(acquired: bool) -> Any:
    lock_ctx = MagicMock()
    lock_ctx.__aenter__ = AsyncMock(return_value=acquired)
    lock_ctx.__aexit__ = AsyncMock(return_value=None)
    return lock_ctx


def _make_runtime_mock(
    *,
    ok: int = 2,
    failed: int = 0,
    skipped: int = 1,
) -> tuple[Any, Any]:
    summary = _make_summary(ok=ok, failed=failed, skipped=skipped)
    mock_service = AsyncMock()
    mock_service.perform_sync = AsyncMock(return_value=summary)
    mock_runtime = MagicMock()
    mock_runtime.service = mock_service
    return mock_runtime, summary


# Common patch targets used in task body tests.
# get_redis / RedisDistributedLock are module-level imports in git_backup_sync,
# so they must be patched at app.tasks.git_backup_sync.<name>.
# The ping_* helpers and build_git_backup_task_runtime are lazily imported inside
# the lock body, so they must be patched at their source modules.
_TASK_PATCHES = {
    "get_redis": "app.tasks.git_backup_sync.get_redis",
    "RedisDistributedLock": "app.tasks.git_backup_sync.RedisDistributedLock",
    "build_git_backup_task_runtime": "app.tasks.deps.build_git_backup_task_runtime",
    "ping_start": "app.adapters.git_backup.health_ping.ping_start",
    "ping_success": "app.adapters.git_backup.health_ping.ping_success",
    "ping_failure": "app.adapters.git_backup.health_ping.ping_failure",
    "_enumerate_and_upsert_gists": "app.tasks.git_backup_sync._enumerate_and_upsert_gists",
    "_enumerate_and_upsert_github_repos": "app.tasks.git_backup_sync._enumerate_and_upsert_github_repos",
    "_index_mirror_readmes": "app.tasks.git_backup_sync._index_mirror_readmes",
    "_reconcile_mirror_readmes": "app.tasks.git_backup_sync._reconcile_mirror_readmes",
    "_prune_stale_excluded": "app.tasks.git_backup_sync._prune_stale_excluded",
    "_export_metrics": "app.tasks.git_backup_sync._export_metrics",
    "_send_telegram_notify": "app.tasks.git_backup_sync._send_telegram_notify",
}


class TestSyncGitBackupTask:
    @pytest.mark.asyncio
    async def test_disabled_returns_early(self) -> None:
        """When git_backup.enabled=False the task returns immediately."""
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_ENABLED=False)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()

        with patch(_TASK_PATCHES["get_redis"]) as mock_get_redis:
            await sync_git_backup(cfg=cfg, db=db)
            mock_get_redis.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_already_held_returns_early(self) -> None:
        """When Redis lock is not acquired, task logs and returns."""
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg()
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=False)

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"]) as mock_brt,
        ):
            await sync_git_backup(cfg=cfg, db=db)
            mock_brt.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_runs_perform_sync(self) -> None:
        """With lock acquired and all flags off, perform_sync is called."""
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg()
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], AsyncMock()),
            patch(_TASK_PATCHES["ping_success"], AsyncMock()),
            patch(_TASK_PATCHES["ping_failure"], AsyncMock()),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            await sync_git_backup(cfg=cfg, db=db)

        runtime.service.perform_sync.assert_called_once()

    @pytest.mark.asyncio
    async def test_hc_ping_start_and_success_called(self) -> None:
        """hc_ping_url set -> ping_start and ping_success called."""
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_HC_PING_URL="https://hc-ping.com/test-uuid")
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()
        ping_start = AsyncMock()
        ping_success = AsyncMock()
        ping_failure = AsyncMock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], ping_start),
            patch(_TASK_PATCHES["ping_success"], ping_success),
            patch(_TASK_PATCHES["ping_failure"], ping_failure),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            await sync_git_backup(cfg=cfg, db=db)

        ping_start.assert_called_once()
        ping_success.assert_called_once()
        ping_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_hc_ping_failure_called_on_exception(self) -> None:
        """When perform_sync raises, ping_failure is called and exception re-raised."""
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_HC_PING_URL="https://hc-ping.com/test-uuid")
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)

        broken_service = AsyncMock()
        broken_service.perform_sync = AsyncMock(side_effect=RuntimeError("sync exploded"))
        broken_runtime = MagicMock()
        broken_runtime.service = broken_service

        ping_start = AsyncMock()
        ping_success = AsyncMock()
        ping_failure = AsyncMock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=broken_runtime),
            patch(_TASK_PATCHES["ping_start"], ping_start),
            patch(_TASK_PATCHES["ping_success"], ping_success),
            patch(_TASK_PATCHES["ping_failure"], ping_failure),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="sync exploded"):
                await sync_git_backup(cfg=cfg, db=db)

        ping_failure.assert_called_once()
        ping_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_mirror_gists_flag_triggers_gist_enumeration(self) -> None:
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_MIRROR_GISTS=True)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()
        enum_gists = AsyncMock(return_value=0)

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], AsyncMock()),
            patch(_TASK_PATCHES["ping_success"], AsyncMock()),
            patch(_TASK_PATCHES["ping_failure"], AsyncMock()),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], enum_gists),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            await sync_git_backup(cfg=cfg, db=db)

        enum_gists.assert_called_once_with(cfg, db)

    @pytest.mark.asyncio
    async def test_mirror_starred_flag_triggers_repo_enumeration(self) -> None:
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_MIRROR_STARRED=True)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()
        enum_repos = AsyncMock(return_value=0)

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], AsyncMock()),
            patch(_TASK_PATCHES["ping_success"], AsyncMock()),
            patch(_TASK_PATCHES["ping_failure"], AsyncMock()),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], enum_repos),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            await sync_git_backup(cfg=cfg, db=db)

        enum_repos.assert_called_once_with(cfg, db)

    @pytest.mark.asyncio
    async def test_index_readmes_flag_triggers_indexing(self) -> None:
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_INDEX_READMES=True)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()
        index_readmes = AsyncMock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], AsyncMock()),
            patch(_TASK_PATCHES["ping_success"], AsyncMock()),
            patch(_TASK_PATCHES["ping_failure"], AsyncMock()),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], index_readmes),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            await sync_git_backup(cfg=cfg, db=db)

        index_readmes.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconcile_readmes_flag_triggers_reconciliation(self) -> None:
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_RECONCILE_READMES=True)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()
        reconcile = AsyncMock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], AsyncMock()),
            patch(_TASK_PATCHES["ping_success"], AsyncMock()),
            patch(_TASK_PATCHES["ping_failure"], AsyncMock()),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], reconcile),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            await sync_git_backup(cfg=cfg, db=db)

        reconcile.assert_called_once()

    @pytest.mark.asyncio
    async def test_prune_excluded_days_flag_triggers_prune(self) -> None:
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(GIT_BACKUP_PRUNE_EXCLUDED_DAYS=14)
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()
        prune = AsyncMock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], AsyncMock()),
            patch(_TASK_PATCHES["ping_success"], AsyncMock()),
            patch(_TASK_PATCHES["ping_failure"], AsyncMock()),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], prune),
            patch(_TASK_PATCHES["_export_metrics"], AsyncMock()),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            await sync_git_backup(cfg=cfg, db=db)

        prune.assert_called_once_with(cfg, db)

    @pytest.mark.asyncio
    async def test_exit_on_failure_raises_and_fires_hc_failure_ping(self) -> None:
        """exit_on_failure=True with failed>0 raises RuntimeError and fires failure ping."""
        from app.tasks.git_backup_sync import sync_git_backup

        git_cfg = _make_git_cfg(
            GIT_BACKUP_EXIT_ON_FAILURE=True,
            GIT_BACKUP_HC_PING_URL="https://hc-ping.com/uuid",
        )
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock(ok=1, failed=2)
        ping_start = AsyncMock()
        ping_success = AsyncMock()
        ping_failure = AsyncMock()
        export_metrics = AsyncMock()
        send_notify = AsyncMock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], ping_start),
            patch(_TASK_PATCHES["ping_success"], ping_success),
            patch(_TASK_PATCHES["ping_failure"], ping_failure),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_prune_stale_excluded"], AsyncMock()),
            patch(_TASK_PATCHES["_export_metrics"], export_metrics),
            patch(_TASK_PATCHES["_send_telegram_notify"], send_notify),
        ):
            with pytest.raises(RuntimeError, match="git_backup_sync_failed"):
                await sync_git_backup(cfg=cfg, db=db)

        export_metrics.assert_called_once()
        send_notify.assert_called_once()
        ping_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_prune_exception_is_swallowed_task_continues(self) -> None:
        """Unexpected exception in _prune_stale_excluded is swallowed; task succeeds."""
        from app.tasks.git_backup_sync import sync_git_backup

        # Include hc_ping_url so we can assert ping_success fires (not ping_failure).
        git_cfg = _make_git_cfg(
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=1,
            GIT_BACKUP_HC_PING_URL="https://hc-ping.com/prune-test",
        )
        cfg = _make_app_config(git_cfg)
        db = _make_fake_db()
        lock_ctx = _make_lock_ctx(acquired=True)
        runtime, _summary = _make_runtime_mock()
        ping_success = AsyncMock()
        ping_failure = AsyncMock()
        export_metrics = AsyncMock()

        with (
            patch(_TASK_PATCHES["get_redis"], AsyncMock(return_value=AsyncMock())),
            patch(_TASK_PATCHES["RedisDistributedLock"], return_value=lock_ctx),
            patch(_TASK_PATCHES["build_git_backup_task_runtime"], return_value=runtime),
            patch(_TASK_PATCHES["ping_start"], AsyncMock()),
            patch(_TASK_PATCHES["ping_success"], ping_success),
            patch(_TASK_PATCHES["ping_failure"], ping_failure),
            patch(_TASK_PATCHES["_enumerate_and_upsert_gists"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_enumerate_and_upsert_github_repos"], AsyncMock(return_value=0)),
            patch(_TASK_PATCHES["_index_mirror_readmes"], AsyncMock()),
            patch(_TASK_PATCHES["_reconcile_mirror_readmes"], AsyncMock()),
            patch(
                _TASK_PATCHES["_prune_stale_excluded"], AsyncMock(side_effect=RuntimeError("boom"))
            ),
            patch(_TASK_PATCHES["_export_metrics"], export_metrics),
            patch(_TASK_PATCHES["_send_telegram_notify"], AsyncMock()),
        ):
            # Must not raise — prune errors are wrapped in try/except inside the task.
            await sync_git_backup(cfg=cfg, db=db)

        # Prune error was swallowed: subsequent steps (metrics, ping) still ran.
        export_metrics.assert_called_once()
        ping_success.assert_called_once()
        ping_failure.assert_not_called()
