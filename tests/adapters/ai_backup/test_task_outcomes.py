from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.db.models.ai_backup import (
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)
from app.tasks import ai_backup_sync


class _Repo:
    def __init__(
        self,
        states: dict[AiBackupService, tuple[AiBackupStatus, AiBackupAuthorizationStatus]],
    ) -> None:
        self._states = states

    async def get(self, _owner_id: int, service: AiBackupService) -> SimpleNamespace:
        status, authorization_status = self._states[service]
        return SimpleNamespace(status=status, authorization_status=authorization_status)


class _Service:
    def __init__(self, **_kwargs: object) -> None:
        self.calls: list[AiBackupService] = []

    async def run(self, _owner_id: int, service: AiBackupService) -> None:
        self.calls.append(service)


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.ai_backup.hc_ping_url = None
    return cfg


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    states: dict[AiBackupService, tuple[AiBackupStatus, AiBackupAuthorizationStatus]],
) -> list[tuple[str, str]]:
    import app.adapters.ai_backup.repository as repository_module
    import app.adapters.ai_backup.service as service_module
    import app.adapters.ai_backup.session_store as session_store_module

    repo = _Repo(states)
    monkeypatch.setattr(repository_module, "AiBackupRepository", lambda _db: repo)
    monkeypatch.setattr(session_store_module, "AiBackupSessionStore", lambda _db: MagicMock())
    monkeypatch.setattr(service_module, "AiBackupOrchestrationService", _Service)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ai_backup_sync,
        "record_backup_run",
        lambda backup, outcome: recorded.append((backup, outcome)),
    )
    return recorded


@pytest.mark.asyncio
async def test_run_sync_raises_after_recording_every_non_successful_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_runtime(
        monkeypatch,
        {
            AiBackupService.CHATGPT: (
                AiBackupStatus.FAILED,
                AiBackupAuthorizationStatus.EXPIRED,
            ),
            AiBackupService.CLAUDE: (
                AiBackupStatus.OK,
                AiBackupAuthorizationStatus.VALID,
            ),
        },
    )

    with pytest.raises(RuntimeError, match="chatgpt"):
        await ai_backup_sync._run_sync(
            _config(),
            MagicMock(),
            owner_id=1,
            services=[AiBackupService.CHATGPT, AiBackupService.CLAUDE],
        )

    assert recorded == [("chatgpt", "auth_required"), ("claude", "success")]


@pytest.mark.asyncio
async def test_run_sync_completes_only_when_all_services_are_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _patch_runtime(
        monkeypatch,
        {
            AiBackupService.CHATGPT: (
                AiBackupStatus.OK,
                AiBackupAuthorizationStatus.VALID,
            ),
            AiBackupService.CLAUDE: (
                AiBackupStatus.OK,
                AiBackupAuthorizationStatus.VALID,
            ),
        },
    )

    await ai_backup_sync._run_sync(
        _config(),
        MagicMock(),
        owner_id=1,
        services=[AiBackupService.CHATGPT, AiBackupService.CLAUDE],
    )

    assert recorded == [("chatgpt", "success"), ("claude", "success")]
