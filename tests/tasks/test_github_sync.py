"""Tests for app.tasks.github_sync — analysis budget, watches and scheduling."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.tasks.github_sync_helpers import (
    _build_cfg,
    _evict_task_modules,
    _make_integration,
    _make_repo,
    _RecordingMetric,
    _stub_taskiq,
)


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
async def test_rate_limited_user_increments_sync_metrics(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.adapters.github.exceptions import GitHubRateLimitError
    from app.tasks.github_sync import _sync_all

    integration = _make_integration()
    integration.last_sync_cursor = None

    class _TxnSession:
        async def get(self, model, pk):
            return integration

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.transaction = MagicMock(side_effect=_TxnSession)

    rate_limited = _RecordingMetric()
    streak = _RecordingMetric()
    runs = _RecordingMetric()

    with (
        patch(
            "app.tasks.github_sync._sync_one_integration",
            side_effect=GitHubRateLimitError(reset_epoch=1_735_689_600),
        ),
        patch("app.tasks.github_sync.GITHUB_SYNC_RATE_LIMITED_TOTAL", rate_limited),
        patch("app.tasks.github_sync.GITHUB_SYNC_RATE_LIMIT_STREAK", streak),
        patch("app.tasks.github_sync.GITHUB_SYNC_RUNS_TOTAL", runs),
        patch("app.tasks.github_sync.GITHUB_SYNC_REPOS_IMPORTED_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_SYNC_REPOS_UPDATED_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_SYNC_REPOS_UNSTARRED_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_SYNC_LLM_CALLS_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_PENDING_ANALYSIS_BACKLOG", None),
    ):
        result = await _sync_all(
            [integration],
            cfg=_build_cfg(),
            db=db,
            bot=None,
            correlation_id="test-cid",
        )

    assert result.errors_per_user == {42: "rate_limit reset=1735689600"}
    assert rate_limited.inc_calls == [({"user_id": "42"}, 1.0)]
    assert streak.set_calls == [({"user_id": "42"}, 1)]
    assert runs.inc_calls == [({"status": "ratelimited"}, 1.0)]


@pytest.mark.asyncio
async def test_rate_limited_user_does_not_block_other_users_and_resumes_next_run(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.adapters.github.exceptions import GitHubRateLimitError
    from app.tasks.github_sync import _sync_all

    user_a = _make_integration(user_id=1001)
    user_a.id = 1
    user_a.last_sync_cursor = None
    user_b = _make_integration(user_id=1002)
    user_b.id = 2
    user_b.last_sync_cursor = None
    integrations_by_id = {1: user_a, 2: user_b}
    processed: list[int] = []

    class _TxnSession:
        async def get(self, model, pk):
            return integrations_by_id.get(pk)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    db = MagicMock()
    db.transaction = MagicMock(side_effect=_TxnSession)

    async def _fake_sync_one_integration(*, integration, **kwargs):
        processed.append(integration.user_id)
        if integration.user_id == user_a.user_id and processed.count(user_a.user_id) == 1:
            raise GitHubRateLimitError(reset_epoch=1)
        integration.last_sync_cursor = None
        return (1, 0, 0, 1, 0, 0, 0)

    with (
        patch(
            "app.tasks.github_sync._sync_one_integration", side_effect=_fake_sync_one_integration
        ),
        patch("app.tasks.github_sync.GITHUB_SYNC_RATE_LIMITED_TOTAL", _RecordingMetric()),
        patch("app.tasks.github_sync.GITHUB_SYNC_RATE_LIMIT_STREAK", _RecordingMetric()),
        patch("app.tasks.github_sync.GITHUB_SYNC_RUNS_TOTAL", _RecordingMetric()),
        patch("app.tasks.github_sync.GITHUB_SYNC_REPOS_IMPORTED_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_SYNC_REPOS_UPDATED_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_SYNC_REPOS_UNSTARRED_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_SYNC_LLM_CALLS_TOTAL", None),
        patch("app.tasks.github_sync.GITHUB_PENDING_ANALYSIS_BACKLOG", None),
    ):
        first = await _sync_all(
            [user_a, user_b],
            cfg=_build_cfg(),
            db=db,
            bot=None,
            correlation_id="first-run",
        )
        assert first.users_processed == 2
        assert first.repos_imported == 1
        assert first.llm_calls_made == 1
        assert first.errors_per_user == {user_a.user_id: "rate_limit reset=1"}
        assert processed == [user_a.user_id, user_b.user_id]
        assert user_a.last_sync_cursor is not None
        assert user_b.last_sync_cursor is None

        second = await _sync_all(
            [user_a, user_b],
            cfg=_build_cfg(),
            db=db,
            bot=None,
            correlation_id="second-run",
        )

    assert second.users_processed == 2
    assert second.errors_per_user == {}
    assert second.repos_imported == 2
    assert processed == [user_a.user_id, user_b.user_id, user_a.user_id, user_b.user_id]
    assert user_a.last_sync_cursor is None


def test_repository_watch_first_observation_records_baseline_without_event(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _repository_watch_events_for_state

    events = _repository_watch_events_for_state(
        user_id=42,
        repository_id=1,
        repository_full_name="owner/repo",
        repository_url="https://github.com/owner/repo",
        release_url=None,
        watch_readme=True,
        watch_releases=True,
        previous_readme_sha256=None,
        last_notified_readme_sha256=None,
        current_readme_sha256="hash-a",
        previous_release_tag=None,
        last_notified_release_tag=None,
        current_release_tag="v1.0.0",
    )

    assert events == []


def test_repository_watch_readme_change_triggers_once_per_hash(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _repository_watch_events_for_state

    events = _repository_watch_events_for_state(
        user_id=42,
        repository_id=1,
        repository_full_name="owner/repo",
        repository_url="https://github.com/owner/repo",
        release_url=None,
        watch_readme=True,
        watch_releases=False,
        previous_readme_sha256="hash-a",
        last_notified_readme_sha256=None,
        current_readme_sha256="hash-b",
        previous_release_tag=None,
        last_notified_release_tag=None,
        current_release_tag=None,
    )
    duplicate = _repository_watch_events_for_state(
        user_id=42,
        repository_id=1,
        repository_full_name="owner/repo",
        repository_url="https://github.com/owner/repo",
        release_url=None,
        watch_readme=True,
        watch_releases=False,
        previous_readme_sha256="hash-a",
        last_notified_readme_sha256="hash-b",
        current_readme_sha256="hash-b",
        previous_release_tag=None,
        last_notified_release_tag=None,
        current_release_tag=None,
    )

    assert [event.trigger for event in events] == ["readme"]
    assert duplicate == []


def test_repository_watch_release_change_triggers_once_per_tag(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _repository_watch_events_for_state

    events = _repository_watch_events_for_state(
        user_id=42,
        repository_id=1,
        repository_full_name="owner/repo",
        repository_url="https://github.com/owner/repo",
        release_url="https://github.com/owner/repo/releases/tag/v2.0.0",
        watch_readme=False,
        watch_releases=True,
        previous_readme_sha256=None,
        last_notified_readme_sha256=None,
        current_readme_sha256=None,
        previous_release_tag="v1.0.0",
        last_notified_release_tag=None,
        current_release_tag="v2.0.0",
    )
    duplicate = _repository_watch_events_for_state(
        user_id=42,
        repository_id=1,
        repository_full_name="owner/repo",
        repository_url="https://github.com/owner/repo",
        release_url="https://github.com/owner/repo/releases/tag/v2.0.0",
        watch_readme=False,
        watch_releases=True,
        previous_readme_sha256=None,
        last_notified_readme_sha256=None,
        current_readme_sha256=None,
        previous_release_tag="v1.0.0",
        last_notified_release_tag="v2.0.0",
        current_release_tag="v2.0.0",
    )

    assert [event.trigger for event in events] == ["release"]
    assert duplicate == []


def test_repository_watch_first_release_after_empty_baseline_triggers(monkeypatch):
    _stub_taskiq(monkeypatch)
    _evict_task_modules()
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    from app.tasks.github_sync import _repository_watch_events_for_state

    events = _repository_watch_events_for_state(
        user_id=42,
        repository_id=1,
        repository_full_name="owner/repo",
        repository_url="https://github.com/owner/repo",
        release_url="https://github.com/owner/repo/releases/tag/v1.0.0",
        watch_readme=False,
        watch_releases=True,
        previous_readme_sha256=None,
        last_notified_readme_sha256=None,
        current_readme_sha256=None,
        previous_release_tag="",
        last_notified_release_tag=None,
        current_release_tag="v1.0.0",
    )

    assert [event.trigger for event in events] == ["release"]


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
        return (0, 0, 0, 0, 0, 0, 0)

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


# ---------------------------------------------------------------------------
# last_sync_cursor payload must fit String(500) whatever the error text is
# ---------------------------------------------------------------------------


def test_error_payload_always_fits_the_cursor_column() -> None:
    """The write happens inside the except block that records the failure.

    Truncating only the message left the JSON wrapper (~120 chars) and
    json.dumps escaping outside the budget, so a long DBAPIError or
    ValidationError text produced a payload over String(500). Postgres raised
    22001, the transaction rolled back -- losing last_error and backoff_until --
    the per-integration loop unwound, and the task crashed all three retries.
    """
    from app.tasks.github_sync import _SYNC_CURSOR_MAX_CHARS, _github_sync_error_payload

    cases = [
        "x" * 5000,  # plain overflow
        '"' * 2000,  # every char doubles when escaped
        "\n" * 2000,  # newlines escape to two chars
        "ошибка " * 500,  # non-ASCII -> \uXXXX, six chars each
        "e" * 379,  # just under the old breaking point
        "",  # degenerate
    ]
    for error in cases:
        payload = _github_sync_error_payload(error=error, failure_count=1)
        assert len(payload) <= _SYNC_CURSOR_MAX_CHARS, (
            f"payload of {len(payload)} chars overflows the column for {error[:20]!r}"
        )
        # Still valid JSON carrying the fields the diagnostics reader expects.
        parsed = json.loads(payload)
        assert parsed["kind"] == "github_sync_state"
        assert parsed["failure_count"] == 1
        assert "backoff_until" in parsed


def test_error_payload_keeps_short_messages_intact() -> None:
    from app.tasks.github_sync import _github_sync_error_payload

    payload = json.loads(_github_sync_error_payload(error="boom", failure_count=2))
    assert payload["last_error"] == "boom"


# ---------------------------------------------------------------------------
# One unreachable watched repository must not abandon the rest of the pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_watch_failure_is_isolated_per_repo(monkeypatch) -> None:
    """A 403/502 on one watched repo used to abort the whole watch pass.

    _sync_repository_watches runs *before* the run's timestamp bookkeeping, so
    the exception also left last_synced_at and last_full_sync_at unstamped and
    forced the next run to paginate the full snapshot again.
    """
    from contextlib import asynccontextmanager

    from app.tasks import github_sync as mod

    watches = [
        (
            SimpleNamespace(
                id=1,
                watch_readme=True,
                watch_releases=False,
                last_readme_sha256=None,
                last_notified_readme_sha256=None,
                last_release_tag=None,
                last_notified_release_tag=None,
            ),
            SimpleNamespace(
                id=10,
                owner="acme",
                name="broken",
                full_name="acme/broken",
                url="u",
                default_branch="main",
            ),
        ),
        (
            SimpleNamespace(
                id=2,
                watch_readme=True,
                watch_releases=False,
                last_readme_sha256=None,
                last_notified_readme_sha256=None,
                last_release_tag=None,
                last_notified_release_tag=None,
            ),
            SimpleNamespace(
                id=11,
                owner="acme",
                name="fine",
                full_name="acme/fine",
                url="u",
                default_branch="main",
            ),
        ),
    ]

    class _Session:
        async def execute(self, _stmt):
            return SimpleNamespace(all=lambda: watches)

    @asynccontextmanager
    async def _session():
        yield _Session()

    db = SimpleNamespace(session=_session)

    attempted: list[str] = []

    class _Client:
        async def get_readme(self, owner, name, ref=None):
            attempted.append(name)
            if name == "broken":
                raise RuntimeError("GitHub returned 403 Forbidden")
            return SimpleNamespace(content="body")

    updated: list[int] = []

    async def _fake_update(_db, *, watch_id, **_kw):
        updated.append(watch_id)

    monkeypatch.setattr(mod, "_update_repository_watch_state", _fake_update)
    monkeypatch.setattr(mod, "_mark_repository_watch_checked", AsyncMock())
    monkeypatch.setattr(mod, "_emit_repository_watch_triggered", AsyncMock())

    await mod._sync_repository_watches(
        client=_Client(), db=db, user_id=1, bot=None, correlation_id="cid"
    )

    # Both repos were attempted, and the healthy one still committed its state.
    assert attempted == ["broken", "fine"]
    assert updated == [2]
