"""Tests for app.tasks.github_sync — star listing, unstarring and star lists."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.tasks.github_sync_helpers import (
    _async_iter,
    _build_cfg,
    _evict_task_modules,
    _make_integration,
    _make_starred_item,
    _stub_taskiq,
)


@pytest.mark.asyncio
async def test_sync_imports_new_starred_repos(monkeypatch):
    """3 new starred items → 3 Repository rows created; analyze called 3 times."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_one_integration

    integration = _make_integration()
    starred_items = [
        _make_starred_item(github_id=1001, name="repo1"),
        _make_starred_item(github_id=1002, name="repo2"),
        _make_starred_item(github_id=1003, name="repo3"),
    ]

    # Build a fake DB that returns "no existing" for each upsert query
    created_rows = []

    class _FakeSession:
        def __init__(self):
            self._rows = {}

        async def execute(self, stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None  # always new
            return result

        async def flush(self):
            pass

        def add(self, row):
            created_rows.append(row)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, model, pk):
            return MagicMock()  # integration row update

    db = MagicMock()
    db.session = MagicMock(side_effect=_FakeSession)
    db.transaction = MagicMock(side_effect=_FakeSession)

    analyze_calls = []

    async def _fake_analyze(repo_id, *, correlation_id, chosen_lang="en"):
        analyze_calls.append(repo_id)
        return MagicMock(cached=False)

    fake_use_case = MagicMock()
    fake_use_case.analyze = _fake_analyze

    with (
        patch("app.tasks.github_sync.decrypt_token", return_value="ghp_fake"),
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch(
            "app.adapters.github.github_api_client.GitHubAPIClient.__aenter__",
            return_value=MagicMock(list_starred=AsyncMock(return_value=_async_iter(starred_items))),
        ),
    ):
        # Patch GitHubAPIClient directly
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)
        fake_client.list_starred = AsyncMock(return_value=_async_iter(starred_items))

        with patch("app.tasks.github_sync.GitHubAPIClient", return_value=fake_client):
            imported, updated, *_rest = await _sync_one_integration(
                integration=integration,
                cfg=_build_cfg(),
                db=db,
                bot=None,
                correlation_id="test-cid",
            )

    assert imported == 3
    assert updated == 0
    assert len(analyze_calls) == 3


@pytest.mark.asyncio
async def test_sync_unstars_repos_no_longer_starred(monkeypatch):
    """2 repos in DB starred, API returns 1 → the missing one is counted as unstarred."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_one_integration

    integration = _make_integration()
    # API only returns repo1001; repo1002 should be unstarred
    starred_items = [_make_starred_item(github_id=1001, name="repo1")]

    class _SimpleRow:
        def __init__(self, repo_id: int):
            self.id = repo_id
            self.is_starred = True
            self.last_synced_at = None
            self.last_full_sync_at = None
            self.notified_needs_reauth_at = None

    row_by_pk: dict[int, _SimpleRow] = {
        integration.id: _SimpleRow(integration.id),
    }

    # Tracks execute() calls on transaction sessions so we can verify the
    # bulk UPDATE was issued (rather than per-row get+set).
    update_execute_calls: list = []

    class _TxnSession:
        async def execute(self, stmt):
            update_execute_calls.append(stmt)
            r = MagicMock()
            r.scalar_one_or_none.return_value = None  # treat repo as new
            # For the bulk UPDATE ... RETURNING, return one unstarred row id.
            r.fetchall.return_value = [(1002,)]
            return r

        async def flush(self):
            pass

        def add(self, row):
            pass

        async def get(self, model, pk):
            if pk not in row_by_pk:
                row_by_pk[pk] = _SimpleRow(pk)
            return row_by_pk[pk]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class _ReadSession:
        """db.session() — used for per-repo existence lookups."""

        async def execute(self, stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = None  # always new
            return r

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.session = MagicMock(side_effect=_ReadSession)
    db.transaction = MagicMock(side_effect=_TxnSession)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.list_starred = AsyncMock(return_value=_async_iter(starred_items))

    fake_use_case = MagicMock()
    fake_use_case.analyze = AsyncMock(return_value=MagicMock(cached=False))

    with (
        patch("app.tasks.github_sync.decrypt_token", return_value="ghp_fake"),
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch("app.tasks.github_sync.GitHubAPIClient", return_value=fake_client),
    ):
        _imported, _updated, unstarred, *_rest = await _sync_one_integration(
            integration=integration,
            cfg=_build_cfg(),
            db=db,
            bot=None,
            correlation_id="test-cid",
        )

    # repo1002 was not returned by API → bulk UPDATE issued, count=1
    assert unstarred == 1


@pytest.mark.asyncio
async def test_incremental_sync_does_not_unstar_repos_not_returned(monkeypatch):
    """Incremental GitHub starred pages are not a full snapshot and must not unstar misses."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_one_integration

    # Snapshot taken just now, so this run takes the incremental branch.
    integration = _make_integration(
        last_synced_at=datetime(2024, 5, 1, tzinfo=UTC),
        last_full_sync_at=datetime.now(UTC),
    )
    starred_items = [_make_starred_item(github_id=1001, name="repo1")]
    update_execute_calls: list[object] = []

    class _TxnSession:
        async def execute(self, stmt):
            update_execute_calls.append(stmt)
            r = MagicMock()
            r.fetchall.return_value = [(1002,)]
            return r

        async def flush(self):
            pass

        def add(self, row):
            pass

        async def get(self, model, pk):
            return MagicMock(last_sync_cursor=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class _ReadSession:
        async def execute(self, stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.session = MagicMock(side_effect=_ReadSession)
    db.transaction = MagicMock(side_effect=_TxnSession)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.list_starred = AsyncMock(return_value=_async_iter(starred_items))

    fake_use_case = MagicMock()
    fake_use_case.analyze = AsyncMock(return_value=MagicMock(cached=False))

    with (
        patch("app.tasks.github_sync.decrypt_token", return_value="ghp_fake"),
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch("app.tasks.github_sync.GitHubAPIClient", return_value=fake_client),
    ):
        _imported, _updated, unstarred, *_rest = await _sync_one_integration(
            integration=integration,
            cfg=_build_cfg(),
            db=db,
            bot=None,
            correlation_id="test-cid",
        )

    assert unstarred == 0
    assert fake_client.list_starred.await_args.kwargs["since"] == integration.last_synced_at
    assert len(update_execute_calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_age_days", "force_full"),
    [
        (8, False),  # stale stamp — the periodic snapshot is due
        (0, True),  # fresh stamp, but --full overrides it
    ],
    ids=["interval_due", "force_full"],
)
async def test_full_snapshot_pages_whole_listing_and_unstars(
    monkeypatch, snapshot_age_days, force_full
):
    """A snapshot run pages everything (since=None) and soft-unstars the misses."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_one_integration

    now = datetime.now(UTC)
    integration = _make_integration(
        last_synced_at=now - timedelta(days=1),
        last_full_sync_at=now - timedelta(days=snapshot_age_days),
    )
    starred_items = [_make_starred_item(github_id=1001, name="repo1")]
    integ_row = MagicMock(last_sync_cursor=None, last_full_sync_at=None)

    class _TxnSession:
        async def execute(self, stmt):
            r = MagicMock()
            r.fetchall.return_value = [(1002,)]
            return r

        async def flush(self):
            pass

        def add(self, row):
            pass

        async def get(self, model, pk):
            return integ_row

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class _ReadSession:
        async def execute(self, stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            return r

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.session = MagicMock(side_effect=_ReadSession)
    db.transaction = MagicMock(side_effect=_TxnSession)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.list_starred = AsyncMock(return_value=_async_iter(starred_items))

    fake_use_case = MagicMock()
    fake_use_case.analyze = AsyncMock(return_value=MagicMock(cached=False))

    with (
        patch("app.tasks.github_sync.decrypt_token", return_value="ghp_fake"),
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch("app.tasks.github_sync.GitHubAPIClient", return_value=fake_client),
    ):
        _imported, _updated, unstarred, *_rest = await _sync_one_integration(
            integration=integration,
            cfg=_build_cfg(),
            db=db,
            bot=None,
            correlation_id="test-cid",
            force_full=force_full,
        )

    assert fake_client.list_starred.await_args.kwargs["since"] is None
    assert unstarred == 1
    assert integ_row.last_full_sync_at is not None


@pytest.mark.asyncio
async def test_sync_star_lists_writes_membership_and_clears_dropped(monkeypatch):
    """Every row is reconciled: joined lists get names, dropped rows get []."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.adapters.github.types import StarListDTO
    from app.tasks.github_sync import _sync_star_lists

    # 1001: never tagged, now in two lists. 1002: had no list, joined one.
    # 1003: dropped from every list. 1004: unchanged and must not be rewritten.
    rows = [
        (1, 1001, None),
        (2, 1002, []),
        (3, 1003, ["Obsidian"]),
        (4, 1004, ["InfoSec"]),
    ]
    updates: list = []

    class _TxnSession:
        async def execute(self, stmt):
            if str(stmt).startswith("UPDATE"):
                updates.append(stmt)
                return MagicMock()
            r = MagicMock()
            r.all.return_value = rows
            return r

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.transaction = MagicMock(side_effect=_TxnSession)

    fake_gql = MagicMock()
    fake_gql.__aenter__ = AsyncMock(return_value=fake_gql)
    fake_gql.__aexit__ = AsyncMock(return_value=False)
    fake_gql.fetch_star_lists = AsyncMock(
        return_value=[
            StarListDTO(name="Android", slug="android", repo_github_ids=[1001, 1002]),
            StarListDTO(name="KMP", slug="kmp", repo_github_ids=[1001]),
            StarListDTO(name="InfoSec", slug="infosec", repo_github_ids=[1004]),
        ]
    )

    with patch("app.tasks.github_sync.GitHubGraphQLClient", return_value=fake_gql):
        lists_seen, repos_tagged = await _sync_star_lists(
            token="ghp_fake",
            db=db,
            user_id=42,
            correlation_id="test-cid",
        )

    assert lists_seen == 3
    # 1001 gains two names, 1002 gains one, 1003 is cleared; 1004 is unchanged.
    assert repos_tagged == 3
    assert len(updates) == 3


@pytest.mark.asyncio
async def test_sync_star_lists_dry_run_writes_nothing(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.adapters.github.types import StarListDTO
    from app.tasks.github_sync import _sync_star_lists

    db = MagicMock()
    db.transaction = MagicMock(side_effect=AssertionError("dry run must not open a transaction"))

    fake_gql = MagicMock()
    fake_gql.__aenter__ = AsyncMock(return_value=fake_gql)
    fake_gql.__aexit__ = AsyncMock(return_value=False)
    fake_gql.fetch_star_lists = AsyncMock(
        return_value=[StarListDTO(name="Android", slug="android", repo_github_ids=[1001])]
    )

    with patch("app.tasks.github_sync.GitHubGraphQLClient", return_value=fake_gql):
        lists_seen, repos_tagged = await _sync_star_lists(
            token="ghp_fake",
            db=db,
            user_id=42,
            correlation_id="test-cid",
            dry_run=True,
        )

    assert (lists_seen, repos_tagged) == (1, 1)


@pytest.mark.asyncio
async def test_star_list_failure_does_not_fail_star_sync(monkeypatch):
    """A GraphQL outage must not lose the star sync that already committed."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_one_integration

    integration = _make_integration(last_synced_at=datetime.now(UTC) - timedelta(days=1))
    integration.last_full_sync_at = datetime.now(UTC)
    starred_items = [_make_starred_item(github_id=1001, name="repo1")]

    class _Session:
        async def execute(self, stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = None
            r.fetchall.return_value = []
            return r

        async def flush(self):
            pass

        def add(self, row):
            pass

        async def get(self, model, pk):
            return MagicMock(last_sync_cursor=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.session = MagicMock(side_effect=_Session)
    db.transaction = MagicMock(side_effect=_Session)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.list_starred = AsyncMock(return_value=_async_iter(starred_items))

    fake_use_case = MagicMock()
    fake_use_case.analyze = AsyncMock(return_value=MagicMock(cached=False))

    with (
        patch("app.tasks.github_sync.decrypt_token", return_value="ghp_fake"),
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch("app.tasks.github_sync.GitHubAPIClient", return_value=fake_client),
        patch(
            "app.tasks.github_sync._sync_star_lists",
            AsyncMock(side_effect=RuntimeError("graphql down")),
        ),
    ):
        (
            imported,
            _updated,
            _unstarred,
            _made,
            _deferred,
            star_lists,
            tagged,
        ) = await _sync_one_integration(
            integration=integration,
            cfg=_build_cfg(sync_star_lists=True),
            db=db,
            bot=None,
            correlation_id="test-cid",
        )

    assert imported == 1
    assert (star_lists, tagged) == (0, 0)


def test_needs_full_star_snapshot_branches(monkeypatch):
    """Snapshot due on first sync, on a missing/stale stamp; not when fresh or disabled."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _needs_full_star_snapshot

    now = datetime(2026, 7, 29, tzinfo=UTC)
    never_synced = _make_integration()
    missing_stamp = _make_integration(last_synced_at=now - timedelta(days=1))
    stale = _make_integration(
        last_synced_at=now - timedelta(days=1),
        last_full_sync_at=now - timedelta(days=7),
    )
    fresh = _make_integration(
        last_synced_at=now - timedelta(days=1),
        last_full_sync_at=now - timedelta(days=6),
    )

    assert _needs_full_star_snapshot(never_synced, interval_days=7, now=now) is True
    assert _needs_full_star_snapshot(missing_stamp, interval_days=7, now=now) is True
    assert _needs_full_star_snapshot(stale, interval_days=7, now=now) is True
    assert _needs_full_star_snapshot(fresh, interval_days=7, now=now) is False
    # interval_days=0 disables the periodic refresh but never blocks the first sync.
    assert _needs_full_star_snapshot(stale, interval_days=0, now=now) is False
    assert _needs_full_star_snapshot(never_synced, interval_days=0, now=now) is True
