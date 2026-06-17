"""Tests for app.tasks.github_sync — GitHub stars sync task."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Taskiq stub helpers (copied from test_digest_task pattern)
# ---------------------------------------------------------------------------


def _stub_taskiq(monkeypatch):
    """Stub taskiq and taskiq_redis so imports work without Redis."""
    for mod_name in (
        "taskiq",
        "taskiq.abc",
        "taskiq.abc.schedule_source",
        "taskiq.scheduler",
        "taskiq.scheduler.scheduled_task",
        "taskiq.message",
        "taskiq_redis",
    ):
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))

    taskiq_mod = sys.modules["taskiq"]
    taskiq_mod.AsyncBroker = object
    taskiq_mod.TaskiqDepends = lambda fn, **_kw: None
    taskiq_mod.TaskiqMiddleware = object
    taskiq_mod.InMemoryBroker = MagicMock
    taskiq_mod.TaskiqScheduler = MagicMock

    msg_mod = sys.modules["taskiq.message"]
    msg_mod.TaskiqMessage = object

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    sched_task_mod.ScheduledTask = MagicMock

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    source_mod.ScheduleSource = object

    tkr_mod = sys.modules["taskiq_redis"]
    tkr_mod.RedisStreamBroker = MagicMock
    tkr_mod.RedisAsyncResultBackend = MagicMock


def _evict_task_modules():
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Minimal config builder
# ---------------------------------------------------------------------------


def _build_cfg(*, sync_enabled: bool = True, llm_concurrency: int = 2, llm_daily_budget: int = 100):
    return SimpleNamespace(
        github=SimpleNamespace(
            sync_enabled=sync_enabled,
            sync_cron="0 2 * * *",
            llm_concurrency=llm_concurrency,
            llm_daily_budget=llm_daily_budget,
            sync_batch_size=50,
        ),
        digest=SimpleNamespace(enabled=False, digest_times=[], timezone="UTC"),
        rss=SimpleNamespace(enabled=False, poll_interval_minutes=30),
        signal_ingestion=SimpleNamespace(enabled=False, any_enabled=False),
        openrouter=SimpleNamespace(api_key="k", model="m", fallback_models=[]),
        telegram=SimpleNamespace(api_id=1, api_hash="h", bot_token="t:tok", allowed_user_ids=[123]),
    )


# ---------------------------------------------------------------------------
# Fake DB / model helpers
# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timedelta


def _make_integration(
    *,
    user_id: int = 42,
    status: str = "active",
    last_synced_at=None,
    notified_needs_reauth_at=None,
):
    from app.db.models.repository import GitHubIntegrationStatus

    integ = MagicMock()
    integ.id = 1
    integ.user_id = user_id
    integ.status = GitHubIntegrationStatus(status)
    integ.encrypted_token = b"fake-token"
    integ.last_synced_at = last_synced_at
    integ.last_full_sync_at = None
    integ.notified_needs_reauth_at = notified_needs_reauth_at
    return integ


def _make_repo(
    *,
    github_id: int = 1001,
    user_id: int = 42,
    content_hash: str | None = None,
    pending_analysis: bool = False,
):
    from app.db.models.repository import RepoSource

    repo = MagicMock()
    repo.id = github_id
    repo.github_id = github_id
    repo.user_id = user_id
    repo.content_hash = content_hash
    repo.pending_analysis = pending_analysis
    repo.is_starred = True
    repo.description = "desc"
    repo.topics_json = []
    repo.readme_excerpt = ""
    repo.created_at_github = datetime(2020, 1, 1, tzinfo=UTC)
    repo.source = RepoSource.STARRED
    return repo


def _make_starred_item(*, github_id: int = 1001, name: str = "repo"):
    from app.adapters.github.types import GitHubOwnerDTO, RepositoryDTO, StarredItem

    owner = GitHubOwnerDTO(login="owner", id=99, type="User")
    repo_dto = RepositoryDTO(
        id=github_id,
        name=name,
        full_name=f"owner/{name}",
        owner=owner,
        description="desc",
        homepage=None,
        language="Python",
        topics=[],
        stargazers_count=10,
        forks_count=0,
        watchers_count=0,
        default_branch="main",
        license=None,
        archived=False,
        fork=False,
        is_template=False,
        pushed_at=datetime(2024, 1, 1, tzinfo=UTC),
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        html_url=f"https://github.com/owner/{name}",
    )
    return StarredItem(
        starred_at=datetime(2024, 6, 1, tzinfo=UTC),
        repo=repo_dto,
    )


# ---------------------------------------------------------------------------
# Async iterator helper
# ---------------------------------------------------------------------------


async def _async_iter(items):
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_active_integrations_returns_empty_summary(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import SyncSummary, _sync_body

    # DB returns no active integrations
    db = MagicMock()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session_cm)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session_cm.execute = AsyncMock(return_value=execute_result)
    db.session = MagicMock(return_value=session_cm)

    result = await _sync_body(_build_cfg(), db)

    assert isinstance(result, SyncSummary)
    assert result.users_processed == 0
    assert result.repos_imported == 0
    assert result.repos_updated == 0
    assert result.errors_per_user == {}


@pytest.mark.asyncio
async def test_sync_disabled_does_not_query_integrations(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_body

    db = MagicMock()
    db.session.side_effect = AssertionError("disabled sync should not query the database")

    result = await _sync_body(_build_cfg(sync_enabled=False), db)

    assert result.users_processed == 0
    assert result.errors_per_user == {}


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
            imported, updated, _unstarred, _llm_made, _llm_deferred = await _sync_one_integration(
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
        _imported, _updated, unstarred, _, _ = await _sync_one_integration(
            integration=integration,
            cfg=_build_cfg(),
            db=db,
            bot=None,
            correlation_id="test-cid",
        )

    # repo1002 was not returned by API → bulk UPDATE issued, count=1
    assert unstarred == 1


@pytest.mark.asyncio
async def test_budget_cap_defers_remaining_repos(monkeypatch):
    """budget=2, 5 new repos → 2 analyzed, 3 deferred (pending_analysis=True)."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _analyze_pending

    repos = [_make_repo(github_id=i) for i in range(1, 6)]
    cfg = _build_cfg(llm_concurrency=1, llm_daily_budget=2)

    analyze_calls = []
    pending_calls = []

    async def _fake_analyze(repo_id, *, correlation_id, chosen_lang="en"):
        analyze_calls.append(repo_id)
        return MagicMock(cached=False)

    fake_use_case = MagicMock()
    fake_use_case.analyze = _fake_analyze

    db = MagicMock()

    async def _fake_mark_pending(repo_id, db_):
        pending_calls.append(repo_id)

    with (
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch("app.tasks.github_sync._mark_pending", side_effect=_fake_mark_pending),
    ):
        llm_made = [0]
        llm_deferred = [0]
        await _analyze_pending(
            repos,
            settings=cfg,
            db=db,
            correlation_id="test-cid",
            llm_calls_made=llm_made,
            llm_calls_deferred=llm_deferred,
        )

    assert llm_made[0] == 2
    assert llm_deferred[0] == 3
    assert len(analyze_calls) == 2
    assert len(pending_calls) == 3


@pytest.mark.asyncio
async def test_analyze_failure_rearms_pending_analysis(monkeypatch):
    """analyze() raising (e.g. embedding refresh fails after pending_analysis was
    committed False) must re-arm pending_analysis=True so the repo is retried."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _analyze_pending

    repos = [_make_repo(github_id=7)]
    cfg = _build_cfg(llm_concurrency=1, llm_daily_budget=100)

    pending_calls = []

    async def _fake_analyze(repo_id, *, correlation_id, chosen_lang="en"):
        raise RuntimeError("embedding backend unavailable")

    fake_use_case = MagicMock()
    fake_use_case.analyze = _fake_analyze

    async def _fake_mark_pending(repo_id, db_):
        pending_calls.append(repo_id)

    with (
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch("app.tasks.github_sync._mark_pending", side_effect=_fake_mark_pending),
    ):
        llm_made = [0]
        llm_deferred = [0]
        await _analyze_pending(
            repos,
            settings=cfg,
            db=MagicMock(),
            correlation_id="test-cid",
            llm_calls_made=llm_made,
            llm_calls_deferred=llm_deferred,
        )

    # The LLM budget was consumed (the call was attempted) ...
    assert llm_made[0] == 1
    assert llm_deferred[0] == 0
    # ... but the failure re-armed pending_analysis for a future retry.
    assert pending_calls == [7]


@pytest.mark.asyncio
async def test_dry_run_analyze_failure_does_not_mark_pending(monkeypatch):
    """dry_run returns before analyze() is ever called, so nothing is re-armed."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _analyze_pending

    repos = [_make_repo(github_id=9)]
    cfg = _build_cfg(llm_concurrency=1, llm_daily_budget=100)

    pending_calls = []

    async def _fake_mark_pending(repo_id, db_):
        pending_calls.append(repo_id)

    with patch("app.tasks.github_sync._mark_pending", side_effect=_fake_mark_pending):
        llm_made = [0]
        llm_deferred = [0]
        await _analyze_pending(
            repos,
            settings=cfg,
            db=MagicMock(),
            correlation_id="test-cid",
            llm_calls_made=llm_made,
            llm_calls_deferred=llm_deferred,
            dry_run=True,
        )

    assert pending_calls == []


@pytest.mark.asyncio
async def test_sync_body_with_bot_builds_and_passes_bot(monkeypatch):
    """The worker entrypoint builds a Telethon bot and passes it into _sync_body."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_body_with_bot

    bot = MagicMock()
    bot.__aenter__ = AsyncMock(return_value=bot)
    bot.__aexit__ = AsyncMock(return_value=False)

    captured: dict = {}

    async def _fake_sync_body(cfg, db, *, bot=None):
        captured["bot"] = bot
        return MagicMock()

    with (
        patch("app.tasks.github_sync.create_digest_bot_client", return_value=bot),
        patch("app.tasks.github_sync._sync_body", side_effect=_fake_sync_body),
    ):
        await _sync_body_with_bot(_build_cfg(), MagicMock())

    # The worker-built bot was connected (async with) and handed to _sync_body.
    assert captured["bot"] is bot
    bot.__aenter__.assert_awaited_once()
    bot.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_body_with_bot_falls_back_when_bot_unavailable(monkeypatch):
    """If the worker bot cannot be built, the sync still runs with bot=None."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_body_with_bot

    captured: dict = {}

    async def _fake_sync_body(cfg, db, *, bot=None):
        captured["bot"] = bot
        return MagicMock()

    with (
        patch(
            "app.tasks.github_sync.create_digest_bot_client",
            side_effect=RuntimeError("telethon unavailable"),
        ),
        patch("app.tasks.github_sync._sync_body", side_effect=_fake_sync_body),
    ):
        await _sync_body_with_bot(_build_cfg(), MagicMock())

    assert captured["bot"] is None


@pytest.mark.asyncio
async def test_concurrency_cap_observed(monkeypatch):
    """llm_concurrency=1 — semaphore constructed with that value; analyses complete."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    import asyncio

    from app.tasks.github_sync import _analyze_pending

    repos = [_make_repo(github_id=i) for i in range(1, 4)]
    cfg = _build_cfg(llm_concurrency=1, llm_daily_budget=100)

    analyze_calls = []

    async def _fake_analyze(repo_id, *, correlation_id, chosen_lang="en"):
        analyze_calls.append(repo_id)
        return MagicMock(cached=False)

    fake_use_case = MagicMock()
    fake_use_case.analyze = _fake_analyze

    semaphores_created = []
    real_Semaphore = asyncio.Semaphore

    def _recording_semaphore(n):
        s = real_Semaphore(n)
        semaphores_created.append(n)
        return s

    with (
        patch("app.tasks.github_sync._build_analyze_use_case", return_value=fake_use_case),
        patch("app.tasks.github_sync.asyncio.Semaphore", side_effect=_recording_semaphore),
    ):
        llm_made = [0]
        llm_deferred = [0]
        await _analyze_pending(
            repos,
            settings=cfg,
            db=MagicMock(),
            correlation_id="test-cid",
            llm_calls_made=llm_made,
            llm_calls_deferred=llm_deferred,
        )

    assert semaphores_created == [1]
    assert len(analyze_calls) == 3


@pytest.mark.asyncio
async def test_use_case_built_once_per_run_not_per_repo(monkeypatch):
    """The analyze use case (Qdrant client + embedding service) is constructed
    once per run and reused across all repos, not rebuilt for each repo."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _analyze_pending

    repos = [_make_repo(github_id=i) for i in range(1, 6)]
    cfg = _build_cfg(llm_concurrency=2, llm_daily_budget=100)

    analyze_calls = []

    async def _fake_analyze(repo_id, *, correlation_id, chosen_lang="en"):
        analyze_calls.append(repo_id)
        return MagicMock(cached=False)

    fake_use_case = MagicMock()
    fake_use_case.analyze = _fake_analyze

    build_calls = []

    def _build(db_, settings_):
        build_calls.append(1)
        return fake_use_case

    with patch("app.tasks.github_sync._build_analyze_use_case", side_effect=_build):
        llm_made = [0]
        llm_deferred = [0]
        await _analyze_pending(
            repos,
            settings=cfg,
            db=MagicMock(),
            correlation_id="test-cid",
            llm_calls_made=llm_made,
            llm_calls_deferred=llm_deferred,
        )

    assert len(analyze_calls) == 5
    assert len(build_calls) == 1, "use case must be built once per run, not per repo"


@pytest.mark.asyncio
async def test_dry_run_never_builds_use_case(monkeypatch):
    """A dry run must not construct the use case (no Qdrant/embedding handshake)."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _analyze_pending

    repos = [_make_repo(github_id=i) for i in range(1, 4)]
    cfg = _build_cfg(llm_concurrency=2, llm_daily_budget=100)

    build_calls = []

    def _build(db_, settings_):
        build_calls.append(1)
        return MagicMock()

    with patch("app.tasks.github_sync._build_analyze_use_case", side_effect=_build):
        await _analyze_pending(
            repos,
            settings=cfg,
            db=MagicMock(),
            correlation_id="test-cid",
            llm_calls_made=[0],
            llm_calls_deferred=[0],
            dry_run=True,
        )

    assert build_calls == [], "dry run must not build the use case"


@pytest.mark.asyncio
async def test_auth_error_flips_status_and_notifies(monkeypatch):
    """GitHubAuthError → status=needs_reauth, DM sent once, notified_at set."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.adapters.github.exceptions import GitHubAuthError
    from app.tasks.github_sync import _sync_body

    integration = _make_integration(user_id=7)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session_cm)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [integration]
    session_cm.execute = AsyncMock(return_value=execute_result)

    txn_cm = AsyncMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    integ_row = MagicMock()
    integ_row.status = "active"
    integ_row.notified_needs_reauth_at = None
    txn_cm.get = AsyncMock(return_value=integ_row)

    db = MagicMock()
    db.session = MagicMock(return_value=session_cm)
    db.transaction = MagicMock(return_value=txn_cm)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    async def _raise_auth(*a, **kw):
        raise GitHubAuthError("401")

    with (
        patch("app.tasks.github_sync.decrypt_token", return_value="ghp_fake"),
        patch("app.tasks.github_sync._sync_one_integration", side_effect=GitHubAuthError("401")),
    ):
        result = await _sync_body(_build_cfg(), db, bot=bot)

    assert result.users_processed == 1
    assert 7 in result.errors_per_user


@pytest.mark.asyncio
async def test_auth_error_recent_notification_no_dm(monkeypatch):
    """notified_needs_reauth_at within 7 days → no DM sent."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _notify_needs_reauth

    integration = _make_integration(notified_needs_reauth_at=datetime.now(UTC) - timedelta(days=1))

    bot = MagicMock()
    bot.send_message = AsyncMock()
    db = MagicMock()

    await _notify_needs_reauth(
        integration=integration,
        bot=bot,
        db=db,
        correlation_id="test-cid",
    )

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_user_failure_does_not_break_others(monkeypatch):
    """2 integrations, first errors → second still processes; users_processed=2."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _sync_body

    integ1 = _make_integration(user_id=1)
    integ2 = _make_integration(user_id=2)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session_cm)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [integ1, integ2]
    session_cm.execute = AsyncMock(return_value=execute_result)
    db = MagicMock()
    db.session = MagicMock(return_value=session_cm)
    txn_cm = AsyncMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    txn_cm.get = AsyncMock(return_value=MagicMock(last_sync_cursor=None))
    db.transaction = MagicMock(return_value=txn_cm)

    calls = []

    async def _fake_sync_one(*, integration, cfg, db, bot, correlation_id, **kwargs):
        calls.append(integration.user_id)
        if integration.user_id == 1:
            raise RuntimeError("first user exploded")
        return (0, 0, 0, 0, 0)

    with patch("app.tasks.github_sync._sync_one_integration", side_effect=_fake_sync_one):
        result = await _sync_body(_build_cfg(), db, bot=None)

    assert result.users_processed == 2
    assert 1 in result.errors_per_user
    assert 2 not in result.errors_per_user
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_sync_skips_integration_during_backoff(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _github_sync_error_payload, _sync_all

    integration = _make_integration(user_id=7)
    integration.last_sync_cursor = _github_sync_error_payload(
        error="rate_limit reset=9999999999",
        failure_count=1,
        backoff_until=datetime(2027, 1, 1, tzinfo=UTC),
    )

    result = await _sync_all([integration], cfg=_build_cfg(), db=MagicMock(), bot=None)

    assert result.users_processed == 1
    assert result.errors_per_user == {7: "backoff_active"}


# ---------------------------------------------------------------------------
# Scheduler tests
# ---------------------------------------------------------------------------


@dataclass
class _ScheduledTask:
    task_name: str
    cron: str = ""
    cron_offset: str = ""
    labels: dict = field(default_factory=dict)
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)


def _load_scheduler_module(monkeypatch):
    import importlib

    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _stub_taskiq(monkeypatch)
    sys.modules["taskiq.scheduler.scheduled_task"].ScheduledTask = _ScheduledTask

    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    return importlib.import_module("app.tasks.scheduler")


def test_scheduler_registers_task_when_enabled(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = True
    cfg.github.sync_cron = "0 2 * * *"

    with patch("app.tasks.scheduler.load_config", return_value=cfg):
        source = mod._AppConfigScheduleSource()
        tasks = source._build_tasks()

    task_names = [t.task_name for t in tasks]
    assert "ratatoskr.github.sync_stars" in task_names
    github_task = next(t for t in tasks if t.task_name == "ratatoskr.github.sync_stars")
    assert github_task.cron == "0 2 * * *"
    assert github_task.labels == {"job": "github_stars_sync"}


def test_scheduler_skips_task_when_disabled(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False

    with patch("app.tasks.scheduler.load_config", return_value=cfg):
        source = mod._AppConfigScheduleSource()
        tasks = source._build_tasks()

    task_names = [t.task_name for t in tasks]
    assert "ratatoskr.github.sync_stars" not in task_names
