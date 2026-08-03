"""Tests for app.cli.sync_github_stars CLI entry point."""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from argparse import Namespace


# ---------------------------------------------------------------------------
# Taskiq stub helpers (same pattern as test_github_sync.py)
# ---------------------------------------------------------------------------


def _stub_taskiq(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(taskiq_mod, "AsyncBroker", object, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqDepends", lambda fn, **_kw: None, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqMiddleware", object, raising=False)
    monkeypatch.setattr(taskiq_mod, "InMemoryBroker", MagicMock, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqScheduler", MagicMock, raising=False)

    msg_mod = sys.modules["taskiq.message"]
    monkeypatch.setattr(msg_mod, "TaskiqMessage", object, raising=False)

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    monkeypatch.setattr(sched_task_mod, "ScheduledTask", MagicMock, raising=False)

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    monkeypatch.setattr(source_mod, "ScheduleSource", object, raising=False)

    tkr_mod = sys.modules["taskiq_redis"]
    monkeypatch.setattr(tkr_mod, "RedisStreamBroker", MagicMock, raising=False)
    monkeypatch.setattr(tkr_mod, "RedisAsyncResultBackend", MagicMock, raising=False)


def _evict_task_modules() -> None:
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(log_level="INFO"),
        github=SimpleNamespace(
            sync_enabled=True,
            sync_cron="0 2 * * *",
            llm_concurrency=2,
            llm_daily_budget=100,
        ),
        digest=SimpleNamespace(enabled=False),
        rss=SimpleNamespace(enabled=False),
        signal_ingestion=SimpleNamespace(enabled=False, any_enabled=False),
        openrouter=SimpleNamespace(api_key="k", model="m", fallback_models=[]),
        telegram=SimpleNamespace(api_id=1, api_hash="h", bot_token="t:tok", allowed_user_ids=[123]),
        embedding=SimpleNamespace(provider="local"),
        vector_store=SimpleNamespace(environment="test", user_scope="global"),
    )


def _make_integration(*, user_id: int = 42, status: str = "active") -> MagicMock:
    from app.db.models.repository import GitHubIntegrationStatus

    integ = MagicMock()
    integ.id = user_id * 10
    integ.user_id = user_id
    integ.status = GitHubIntegrationStatus(status)
    integ.encrypted_token = b"fake-token"
    integ.last_synced_at = None
    integ.last_full_sync_at = None
    integ.notified_needs_reauth_at = None
    return integ


def _make_db_with_integrations(integrations: list) -> MagicMock:
    """Return a mock DB whose session().execute() yields the given integrations."""
    db = MagicMock()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session_cm)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = integrations
    session_cm.execute = AsyncMock(return_value=execute_result)
    db.session = MagicMock(return_value=session_cm)
    return db


def _make_sync_summary() -> object:
    _stub_taskiq_static()
    from app.tasks.github_sync import SyncSummary

    return SyncSummary(
        users_processed=1,
        repos_imported=2,
        repos_updated=0,
        repos_unstarred=0,
        llm_calls_made=2,
        llm_calls_deferred=0,
    )


def _stub_taskiq_static() -> None:
    """Minimal stub for import without monkeypatch (used in helpers).

    Nothing undoes this one, so it must not displace anything real. Where taskiq
    is installed the import below wins and every ``hasattr`` guard then declines
    to touch it; only a genuinely missing package gets a placeholder. Inserting
    the placeholder unconditionally left ``taskiq.message`` and friends faked for
    the rest of the session.
    """
    for mod_name in (
        "taskiq",
        "taskiq.abc",
        "taskiq.abc.schedule_source",
        "taskiq.scheduler",
        "taskiq.scheduler.scheduled_task",
        "taskiq.message",
        "taskiq_redis",
    ):
        if mod_name in sys.modules:
            continue
        try:
            importlib.import_module(mod_name)
        except ImportError:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    taskiq_mod = sys.modules["taskiq"]
    if not hasattr(taskiq_mod, "TaskiqDepends"):
        taskiq_mod.TaskiqDepends = lambda fn, **_kw: None  # type: ignore[attr-defined]
        taskiq_mod.AsyncBroker = object  # type: ignore[attr-defined]
        taskiq_mod.TaskiqMiddleware = object  # type: ignore[attr-defined]
        taskiq_mod.InMemoryBroker = MagicMock  # type: ignore[attr-defined]
        taskiq_mod.TaskiqScheduler = MagicMock  # type: ignore[attr-defined]

    msg_mod = sys.modules["taskiq.message"]
    if not hasattr(msg_mod, "TaskiqMessage"):
        msg_mod.TaskiqMessage = object  # type: ignore[attr-defined]

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    if not hasattr(sched_task_mod, "ScheduledTask"):
        sched_task_mod.ScheduledTask = MagicMock  # type: ignore[attr-defined]

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    if not hasattr(source_mod, "ScheduleSource"):
        source_mod.ScheduleSource = object  # type: ignore[attr-defined]

    tkr_mod = sys.modules["taskiq_redis"]
    if not hasattr(tkr_mod, "RedisStreamBroker"):
        tkr_mod.RedisStreamBroker = MagicMock  # type: ignore[attr-defined]
        tkr_mod.RedisAsyncResultBackend = MagicMock  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Import the CLI module under test
# ---------------------------------------------------------------------------

import app.cli.sync_github_stars as sync_cli

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_active_integrations_exits_0_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty DB → stdout contains no_active_integrations=true, exit 0."""
    mock_cfg = _build_cfg()
    monkeypatch.setattr(sync_cli, "_prepare_config", lambda _: mock_cfg)
    monkeypatch.setattr(sync_cli, "setup_json_logging", lambda _: None)
    monkeypatch.setattr(
        sync_cli, "build_runtime_database", lambda *a, **kw: _make_db_with_integrations([])
    )

    rc = sync_cli.main(["--log-level", "INFO"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "no_active_integrations=true" in captured.out


def test_user_id_filter_processes_only_one_integration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--user-id matching one of two integrations → _sync_all called with only that one."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()

    mock_cfg = _build_cfg()
    monkeypatch.setattr(sync_cli, "_prepare_config", lambda _: mock_cfg)
    monkeypatch.setattr(sync_cli, "setup_json_logging", lambda _: None)

    integ1 = _make_integration(user_id=10)
    integ2 = _make_integration(user_id=20)

    # DB returns only integ1 when filtered by user_id=10 (SQLAlchemy filter happens
    # in the real DB; here our mock already returns the filtered list)
    monkeypatch.setattr(
        sync_cli,
        "build_runtime_database",
        lambda *a, **kw: _make_db_with_integrations([integ1]),
    )

    sync_all_calls: list[tuple] = []

    async def _fake_sync_all(
        integrations, *, cfg, db, bot=None, correlation_id=None, dry_run=False, force_full=False
    ):
        sync_all_calls.append((integrations, dry_run))
        from app.tasks.github_sync import SyncSummary

        return SyncSummary(
            users_processed=len(integrations),
            repos_imported=0,
            repos_updated=0,
            repos_unstarred=0,
            llm_calls_made=0,
            llm_calls_deferred=0,
        )

    with patch("app.cli.sync_github_stars._sync_all", _fake_sync_all):
        rc = sync_cli.main(["--user-id", "10"])

    assert rc == 0
    assert len(sync_all_calls) == 1
    passed_integrations, passed_dry_run = sync_all_calls[0]
    assert len(passed_integrations) == 1
    assert passed_integrations[0].user_id == 10
    assert passed_dry_run is False


def test_dry_run_no_db_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run passes dry_run=True to _sync_all; _sync_all itself skips writes."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()

    mock_cfg = _build_cfg()
    monkeypatch.setattr(sync_cli, "_prepare_config", lambda _: mock_cfg)
    monkeypatch.setattr(sync_cli, "setup_json_logging", lambda _: None)

    integ = _make_integration(user_id=42)
    monkeypatch.setattr(
        sync_cli,
        "build_runtime_database",
        lambda *a, **kw: _make_db_with_integrations([integ]),
    )

    dry_run_received: list[bool] = []

    async def _fake_sync_all(
        integrations, *, cfg, db, bot=None, correlation_id=None, dry_run=False, force_full=False
    ):
        dry_run_received.append(dry_run)
        from app.tasks.github_sync import SyncSummary

        return SyncSummary(
            users_processed=1,
            repos_imported=2,
            repos_updated=0,
            repos_unstarred=0,
            llm_calls_made=0,
            llm_calls_deferred=2,
        )

    with patch("app.cli.sync_github_stars._sync_all", _fake_sync_all):
        rc = sync_cli.main(["--dry-run"])

    assert rc == 0
    assert dry_run_received == [True]
    captured = capsys.readouterr()
    # stdout contains the summary JSON with would-be counts
    assert "repos_imported" in captured.out
    assert (
        '"repos_imported": 2' in captured.out
        or "'repos_imported': 2" in captured.out
        or "2" in captured.out
    )


def test_full_flag_forces_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--full passes force_full=True to _sync_all; without it the run stays incremental."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()

    mock_cfg = _build_cfg()
    monkeypatch.setattr(sync_cli, "_prepare_config", lambda _: mock_cfg)
    monkeypatch.setattr(sync_cli, "setup_json_logging", lambda _: None)
    monkeypatch.setattr(
        sync_cli,
        "build_runtime_database",
        lambda *a, **kw: _make_db_with_integrations([_make_integration(user_id=42)]),
    )

    force_full_received: list[bool] = []

    async def _fake_sync_all(
        integrations, *, cfg, db, bot=None, correlation_id=None, dry_run=False, force_full=False
    ):
        force_full_received.append(force_full)
        from app.tasks.github_sync import SyncSummary

        return SyncSummary(
            users_processed=1,
            repos_imported=0,
            repos_updated=0,
            repos_unstarred=3,
            llm_calls_made=0,
            llm_calls_deferred=0,
        )

    with patch("app.cli.sync_github_stars._sync_all", _fake_sync_all):
        assert sync_cli.main(["--full"]) == 0
        assert sync_cli.main([]) == 0

    assert force_full_received == [True, False]
    capsys.readouterr()


def test_summary_printed_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Happy path → stdout contains JSON with users_processed, repos_imported."""
    _stub_taskiq(monkeypatch)
    _evict_task_modules()

    import json as _json

    mock_cfg = _build_cfg()
    monkeypatch.setattr(sync_cli, "_prepare_config", lambda _: mock_cfg)
    monkeypatch.setattr(sync_cli, "setup_json_logging", lambda _: None)

    integ = _make_integration(user_id=7)
    monkeypatch.setattr(
        sync_cli,
        "build_runtime_database",
        lambda *a, **kw: _make_db_with_integrations([integ]),
    )

    async def _fake_sync_all(
        integrations, *, cfg, db, bot=None, correlation_id=None, dry_run=False, force_full=False
    ):
        from app.tasks.github_sync import SyncSummary

        return SyncSummary(
            users_processed=1,
            repos_imported=5,
            repos_updated=3,
            repos_unstarred=1,
            llm_calls_made=4,
            llm_calls_deferred=1,
        )

    with patch("app.cli.sync_github_stars._sync_all", _fake_sync_all):
        rc = sync_cli.main([])

    assert rc == 0
    captured = capsys.readouterr()
    data = _json.loads(captured.out)
    assert data["users_processed"] == 1
    assert data["repos_imported"] == 5
    assert "repos_updated" in data
    assert "llm_calls_made" in data


def test_invalid_user_id_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--user-id with no matching active integration → exit non-zero with message."""
    mock_cfg = _build_cfg()
    monkeypatch.setattr(sync_cli, "_prepare_config", lambda _: mock_cfg)
    monkeypatch.setattr(sync_cli, "setup_json_logging", lambda _: None)
    # DB returns empty list for user_id=0
    monkeypatch.setattr(
        sync_cli,
        "build_runtime_database",
        lambda *a, **kw: _make_db_with_integrations([]),
    )

    rc = sync_cli.main(["--user-id", "0"])

    assert rc != 0
    captured = capsys.readouterr()
    assert "user_id=0" in captured.err or "No active" in captured.err


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = sync_cli.parse_args([])
    assert args.user_id is None
    assert args.dry_run is False
    assert args.full is False
    assert args.log_level == "INFO"
    assert args.env_file is None


def test_parse_args_all_flags() -> None:
    from pathlib import Path

    args = sync_cli.parse_args(
        [
            "--user-id",
            "99",
            "--dry-run",
            "--full",
            "--log-level",
            "DEBUG",
            "--env-file",
            "/tmp/.env",
        ]
    )
    assert args.user_id == 99
    assert args.dry_run is True
    assert args.full is True
    assert args.log_level == "DEBUG"
    assert args.env_file == Path("/tmp/.env")


def test_main_returns_one_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_args: Namespace) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sync_cli, "run_sync_cli", _boom)
    rc = sync_cli.main([])
    assert rc == 1
