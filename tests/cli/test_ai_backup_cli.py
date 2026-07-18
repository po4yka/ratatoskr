"""Hermetic unit tests for app.cli.ai_backup.

No real database or network is required. All external collaborators are
replaced with fakes/mocks via monkeypatch on the cli module's namespace.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cli import ai_backup
from app.db.models.ai_backup import (
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_cfg(
    *,
    chatgpt_enabled: bool = True,
    claude_enabled: bool = True,
    owner_ids: tuple[int, ...] = (42,),
) -> SimpleNamespace:
    return SimpleNamespace(
        database=SimpleNamespace(dsn="postgresql+asyncpg://fake/test"),
        telegram=SimpleNamespace(allowed_user_ids=owner_ids),
        ai_backup=SimpleNamespace(
            enabled=True,
            chatgpt_enabled=chatgpt_enabled,
            claude_enabled=claude_enabled,
        ),
    )


def _fake_row(
    status: AiBackupStatus = AiBackupStatus.OK,
    *,
    counts_json: dict | None = None,
    last_backup_path: str | None = "/data/ai-backups/chatgpt/2026-06-27",
    last_error: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        authorization_status=AiBackupAuthorizationStatus.VALID,
        counts_json=counts_json if counts_json is not None else {"conversations": 5},
        last_backup_path=last_backup_path,
        last_error=last_error,
    )


def _patch_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: SimpleNamespace,
    get_return: SimpleNamespace | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Patch all external collaborators on the ai_backup CLI module namespace.

    Returns:
        (fake_db, fake_repo, fake_svc) — the mocks the test assertions target.
    """
    fake_db = MagicMock()
    fake_db.dispose = AsyncMock()

    fake_repo = MagicMock()
    fake_repo.get = AsyncMock(return_value=get_return or _fake_row())

    fake_store = MagicMock()

    fake_svc = MagicMock()
    fake_svc.run = AsyncMock()

    monkeypatch.setattr(ai_backup, "load_config", lambda: cfg)
    monkeypatch.setattr(ai_backup, "Database", lambda config: fake_db)
    monkeypatch.setattr(ai_backup, "AiBackupRepository", lambda db: fake_repo)
    monkeypatch.setattr(ai_backup, "AiBackupSessionStore", lambda db: fake_store)
    monkeypatch.setattr(
        ai_backup,
        "AiBackupOrchestrationService",
        lambda cfg, repo, store, notifier=None: fake_svc,
    )
    return fake_db, fake_repo, fake_svc


# ---------------------------------------------------------------------------
# run_backup() behaviour tests (call the coroutine directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_service_calls_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _fake_cfg()
    fake_db, fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)

    result = await ai_backup.run_backup("chatgpt")

    assert result == 0
    fake_svc.run.assert_awaited_once_with(42, AiBackupService.CHATGPT)
    fake_repo.get.assert_awaited_once_with(42, AiBackupService.CHATGPT)
    fake_db.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_service_claude_calls_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _fake_cfg()
    fake_db, fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)

    result = await ai_backup.run_backup("claude")

    assert result == 0
    fake_svc.run.assert_awaited_once_with(42, AiBackupService.CLAUDE)
    fake_repo.get.assert_awaited_once_with(42, AiBackupService.CLAUDE)
    fake_db.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_enabled_services_runs_both(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _fake_cfg(chatgpt_enabled=True, claude_enabled=True)
    fake_db, _fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)

    result = await ai_backup.run_backup(None)

    assert result == 0
    assert fake_svc.run.await_count == 2
    called_services = [call.args[1] for call in fake_svc.run.await_args_list]
    assert AiBackupService.CHATGPT in called_services
    assert AiBackupService.CLAUDE in called_services
    fake_db.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_enabled_service_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """When claude is disabled, only chatgpt backup is triggered."""
    cfg = _fake_cfg(chatgpt_enabled=True, claude_enabled=False)
    _fake_db, _fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)

    result = await ai_backup.run_backup(None)

    assert result == 0
    fake_svc.run.assert_awaited_once_with(42, AiBackupService.CHATGPT)


@pytest.mark.asyncio
async def test_no_services_enabled_exits_0_without_calling_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg(chatgpt_enabled=False, claude_enabled=False)
    fake_db, _fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)

    result = await ai_backup.run_backup(None)

    assert result == 0
    fake_svc.run.assert_not_awaited()
    assert "No AI backup services are enabled" in capsys.readouterr().err
    fake_db.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_owner_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg(owner_ids=())
    fake_db, _fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)

    result = await ai_backup.run_backup(None)

    assert result == 2
    fake_svc.run.assert_not_awaited()
    assert "ALLOWED_USER_IDS is empty" in capsys.readouterr().err
    fake_db.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_row_status_is_printed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg()
    row = _fake_row(
        AiBackupStatus.OK,
        counts_json={"conversations": 42},
        last_backup_path="/data/ai-backups/chatgpt/2026-06-27",
    )
    _fake_db, _fake_repo, _fake_svc = _patch_deps(monkeypatch, cfg=cfg, get_return=row)

    result = await ai_backup.run_backup("chatgpt")

    out = capsys.readouterr().out
    assert result == 0
    assert "ok" in out
    assert "valid" in out
    assert "42" in out
    assert "2026-06-27" in out


@pytest.mark.asyncio
async def test_none_row_prints_not_found(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg()
    _fake_db, fake_repo, _fake_svc = _patch_deps(monkeypatch, cfg=cfg, get_return=None)
    fake_repo.get = AsyncMock(return_value=None)

    result = await ai_backup.run_backup("chatgpt")

    assert result == 0
    assert "No database row found" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_disabled_service_forced_via_flag_warns_and_runs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--service forces a disabled service to run but emits a warning."""
    cfg = _fake_cfg(claude_enabled=False)
    _fake_db, _fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)

    result = await ai_backup.run_backup("claude")

    assert result == 0
    fake_svc.run.assert_awaited_once_with(42, AiBackupService.CLAUDE)
    assert "not enabled" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_dispose_called_even_when_run_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """db.dispose() must be called in the finally block even on error."""
    cfg = _fake_cfg()
    fake_db, _fake_repo, fake_svc = _patch_deps(monkeypatch, cfg=cfg)
    fake_svc.run = AsyncMock(side_effect=RuntimeError("backup exploded"))

    with pytest.raises(RuntimeError, match="backup exploded"):
        await ai_backup.run_backup("chatgpt")

    fake_db.dispose.assert_awaited_once()


# ---------------------------------------------------------------------------
# main() / argparse surface tests
# ---------------------------------------------------------------------------


def test_main_help_exits_0_and_shows_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ai_backup.sys, "argv", ["ai_backup.py", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        ai_backup.main()
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--service" in out
    assert "--log-level" in out


def test_main_invalid_service_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_backup.sys, "argv", ["ai_backup.py", "--service", "unknown"])
    with pytest.raises(SystemExit) as exc_info:
        ai_backup.main()
    assert exc_info.value.code != 0


def test_main_ingest_without_service_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_backup.sys, "argv", ["ai_backup.py", "--ingest", "x.json"])
    with pytest.raises(SystemExit) as exc_info:
        ai_backup.main()
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# ingest_session() behaviour tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_valid_blob_saves_and_clears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _fake_cfg()
    fake_db = MagicMock()
    fake_db.dispose = AsyncMock()
    fake_store = MagicMock()
    fake_store.save = AsyncMock()
    fake_repo = MagicMock()
    fake_repo.mark_authorization_unverified = AsyncMock()
    monkeypatch.setattr(ai_backup, "load_config", lambda: cfg)
    monkeypatch.setattr(ai_backup, "Database", lambda config: fake_db)
    monkeypatch.setattr(ai_backup, "AiBackupSessionStore", lambda db: fake_store)
    monkeypatch.setattr(ai_backup, "AiBackupRepository", lambda db: fake_repo)

    blob = {
        "cookies": [
            {
                "name": "sessionKey",
                "domain": ".claude.ai",
                "value": "secret",
                "expires": -1,
            }
        ],
        "origins": [],
    }
    path = tmp_path / "claude.json"
    path.write_text(json.dumps(blob))

    result = await ai_backup.ingest_session("claude", str(path))

    assert result == 0
    fake_store.save.assert_awaited_once_with(42, AiBackupService.CLAUDE, blob)
    fake_repo.mark_authorization_unverified.assert_awaited_once_with(42, AiBackupService.CLAUDE)
    fake_db.dispose.assert_awaited_once()
    out = capsys.readouterr().out
    assert "sessionKey" in out  # cookie NAME surfaced
    assert "secret" not in out  # cookie VALUE never printed


@pytest.mark.asyncio
async def test_ingest_bad_json_exits_2(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "x.json"
    path.write_text("{not valid json")
    result = await ai_backup.ingest_session("claude", str(path))
    assert result == 2
    assert "not valid JSON" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_ingest_bad_shape_exits_2(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"no_cookies": True}))
    result = await ai_backup.ingest_session("claude", str(path))
    assert result == 2
    assert "invalid storage_state shape" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_ingest_missing_file_exits_2(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    result = await ai_backup.ingest_session("claude", str(tmp_path / "nope.json"))
    assert result == 2
    assert "cannot read" in capsys.readouterr().err
