"""Tests for the AI backup orchestration service (deps mocked)."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.adapters.ai_backup.errors import (
    AiBackupAuthExpiredError,
    AiBackupErrorCategory,
    AiBackupMaxRequestsError,
)
from app.adapters.ai_backup.service import AiBackupOrchestrationService
from app.config.ai_backup import AiBackupConfig
from app.db.models.ai_backup import (
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)
from app.security.secret_crypto import InvalidEncryptedSecretError

_AC = "app.adapters.content.browser_auth.authenticated_context"
_CF = "app.adapters.ai_backup.client_factory"


class _Row:
    status: AiBackupStatus = AiBackupStatus.PENDING
    authorization_status: AiBackupAuthorizationStatus = AiBackupAuthorizationStatus.UNVERIFIED
    backoff_until: dt.datetime | None = None
    last_backed_up_at: dt.datetime | None = None


class _FakeRepo:
    def __init__(self, row: _Row | None = None) -> None:
        self.row = row or _Row()
        self.calls: list[tuple] = []

    async def ensure(self, _u: int, _s: AiBackupService) -> _Row:
        return self.row

    async def record_success(self, _u, _s, *, counts=None, backup_path=None) -> None:
        self.calls.append(("success", counts, backup_path))

    async def record_failure(self, _u, _s, *, category, message) -> None:
        self.calls.append(("failure", category, message))

    async def mark_auth_expired(self, _u, _s, message) -> None:
        self.calls.append(("auth_expired", message))

    async def mark_authorization_missing(self, _u, _s) -> None:
        self.calls.append(("authorization_missing",))


class _FakeStore:
    def __init__(
        self,
        state: dict | None,
        *,
        load_error: Exception | None = None,
        refresh_present: bool = True,
    ) -> None:
        self._state = state
        self._load_error = load_error
        self._refresh_present = refresh_present
        self.loads: list[tuple[int, AiBackupService]] = []
        self.saved: list[dict] = []

    async def load(self, user_id: int, service: AiBackupService) -> dict | None:
        self.loads.append((user_id, service))
        if self._load_error is not None:
            raise self._load_error
        return self._state

    async def refresh(self, _u, _s, blob) -> bool:
        self.saved.append(blob)
        return self._refresh_present


class _RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def on_start(self, _s) -> None:
        self.events.append("start")

    async def on_success(self, _s, _counts, _cid) -> None:
        self.events.append("success")

    async def on_failure(self, _s, _cid) -> None:
        self.events.append("failure")

    async def on_auth_expired(self, _s, _cid) -> None:
        self.events.append("auth_expired")


class _FakeFetcher:
    def __init__(self, _ctx, **_kw) -> None:
        self.requests_made = 5


def _patch_browser_layer(monkeypatch, client) -> None:
    @asynccontextmanager
    async def _fake_ctx(
        domain, storage_state, *, endpoint_url, mobile=False, proxy="", refreshed_out=None
    ):
        if refreshed_out is not None:
            refreshed_out.append({"cookies": [{"name": "refreshed"}]})
        yield ("page", "ctx")

    monkeypatch.setattr(f"{_AC}.authenticated_context", _fake_ctx)
    monkeypatch.setattr(f"{_AC}.PlaywrightAuthedFetcher", _FakeFetcher)
    monkeypatch.setattr(f"{_CF}.build_client", lambda *a, **k: client)


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        ai_backup=AiBackupConfig(data_path=str(tmp_path)),
        scraper=SimpleNamespace(cloakbrowser_url="http://cloakbrowser:9222"),
    )


class _OkClient:
    skipped = 2

    async def collect(self) -> dict[str, int]:
        return {"conversations": 3, "projects": 1, "files": 0, "artifacts": 4}


async def test_success_path(tmp_path, monkeypatch) -> None:
    repo = _FakeRepo()
    store = _FakeStore({"cookies": []})
    notifier = _RecordingNotifier()
    _patch_browser_layer(monkeypatch, _OkClient())
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    await svc.run(42, AiBackupService.CLAUDE)

    assert any(c[0] == "success" for c in repo.calls)
    assert store.saved == [{"cookies": [{"name": "refreshed"}]}]  # rotated session persisted
    assert notifier.events == ["start", "success"]
    # manifest written under the run dir
    run_dirs = list((tmp_path / "claude").iterdir())
    assert run_dirs and (run_dirs[0] / "manifest.json").exists()


async def test_no_session_returns_early(tmp_path, monkeypatch) -> None:
    repo = _FakeRepo()
    store = _FakeStore(None)
    notifier = _RecordingNotifier()
    _patch_browser_layer(monkeypatch, _OkClient())
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    await svc.run(42, AiBackupService.CLAUDE)
    assert repo.calls == [("authorization_missing",)]
    assert notifier.events == []


async def test_revoke_during_run_is_not_resurrected(tmp_path, monkeypatch) -> None:
    repo = _FakeRepo()
    store = _FakeStore({"cookies": []}, refresh_present=False)
    _patch_browser_layer(monkeypatch, _OkClient())
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, _RecordingNotifier())

    await svc.run(42, AiBackupService.CLAUDE)

    assert [call[0] for call in repo.calls] == ["success", "authorization_missing"]


async def test_backoff_active_returns_early(tmp_path, monkeypatch) -> None:
    row = _Row()
    row.backoff_until = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=1)
    repo = _FakeRepo(row)
    store = _FakeStore({"cookies": []})
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, _RecordingNotifier())
    await svc.run(42, AiBackupService.CLAUDE)
    assert repo.calls == []
    assert store.saved == []


async def test_auth_expired_status_halts_repeated_runs(tmp_path, monkeypatch) -> None:
    row = _Row()
    row.status = AiBackupStatus.OK
    row.authorization_status = AiBackupAuthorizationStatus.EXPIRED
    repo = _FakeRepo(row)
    store = _FakeStore({"cookies": []})
    notifier = _RecordingNotifier()
    _patch_browser_layer(monkeypatch, _OkClient())
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    await svc.run(42, AiBackupService.CLAUDE)
    await svc.run(42, AiBackupService.CLAUDE)

    assert store.loads == []
    assert store.saved == []
    assert repo.calls == []
    assert notifier.events == []


async def test_auth_expired_marks_and_persists_session(tmp_path, monkeypatch) -> None:
    class _AuthClient:
        skipped = 0

        async def collect(self) -> dict[str, int]:
            raise AiBackupAuthExpiredError("401")

    repo = _FakeRepo()
    store = _FakeStore({"cookies": []})
    notifier = _RecordingNotifier()
    _patch_browser_layer(monkeypatch, _AuthClient())
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    await svc.run(42, AiBackupService.CLAUDE)  # must not raise

    assert [c[0] for c in repo.calls] == ["auth_expired"]
    assert notifier.events == ["start", "auth_expired"]
    assert store.saved == [{"cookies": [{"name": "refreshed"}]}]  # cookies still persisted


async def test_rate_limited_writes_partial_manifest_and_reraises(tmp_path, monkeypatch) -> None:
    class _RateLimitedClient:
        skipped = 0

        async def collect(self) -> dict[str, int]:
            raise AiBackupMaxRequestsError("HTTP 429")

    repo = _FakeRepo()
    store = _FakeStore({"cookies": []})
    notifier = _RecordingNotifier()
    _patch_browser_layer(monkeypatch, _RateLimitedClient())
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    with pytest.raises(AiBackupMaxRequestsError):
        await svc.run(42, AiBackupService.CLAUDE)

    # Retryable failure recorded so the scheduler retries (and resumes) after backoff.
    assert repo.calls[0][0] == "failure"
    assert "failure" in notifier.events
    # A partial manifest is written so progress is recorded for the resume.
    run_dirs = list((tmp_path / "claude").iterdir())
    assert run_dirs and (run_dirs[0] / "manifest.json").exists()
    assert store.saved == [{"cookies": [{"name": "refreshed"}]}]


async def test_transient_error_records_failure_and_reraises(tmp_path, monkeypatch) -> None:
    class _BoomClient:
        skipped = 0

        async def collect(self) -> dict[str, int]:
            raise RuntimeError("kaboom")

    repo = _FakeRepo()
    store = _FakeStore({"cookies": []})
    notifier = _RecordingNotifier()
    _patch_browser_layer(monkeypatch, _BoomClient())
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    with pytest.raises(RuntimeError, match="kaboom"):
        await svc.run(42, AiBackupService.CLAUDE)

    assert repo.calls[0][0] == "failure"
    assert repo.calls[0][1] == AiBackupErrorCategory.UNKNOWN
    assert "failure" in notifier.events
    assert store.saved == [{"cookies": [{"name": "refreshed"}]}]


async def test_session_load_error_records_failure(tmp_path) -> None:
    repo = _FakeRepo()
    store = _FakeStore(None, load_error=ValueError("ciphertext is invalid"))
    notifier = _RecordingNotifier()
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    with pytest.raises(ValueError, match="ciphertext is invalid"):
        await svc.run(42, AiBackupService.CLAUDE)

    assert repo.calls[0][0] == "failure"
    assert notifier.events == ["failure"]


async def test_undecryptable_session_requires_reingest_without_overwriting_backup(
    tmp_path,
) -> None:
    row = _Row()
    row.status = AiBackupStatus.OK
    row.authorization_status = AiBackupAuthorizationStatus.VALID
    repo = _FakeRepo(row)
    store = _FakeStore(None, load_error=InvalidEncryptedSecretError("bad key"))
    notifier = _RecordingNotifier()
    svc = AiBackupOrchestrationService(_cfg(tmp_path), repo, store, notifier)

    await svc.run(42, AiBackupService.CLAUDE)

    assert [call[0] for call in repo.calls] == ["auth_expired"]
    assert "bad key" not in repo.calls[0][1]
    assert row.status == AiBackupStatus.OK
    assert notifier.events == ["auth_expired"]


async def test_writer_initialization_error_records_failure(tmp_path, monkeypatch) -> None:
    def _raise_writer(*_args, **_kwargs):
        raise OSError("backup disk unavailable")

    monkeypatch.setattr("app.adapters.ai_backup.disk_writer.AiBackupDiskWriter", _raise_writer)
    repo = _FakeRepo()
    notifier = _RecordingNotifier()
    svc = AiBackupOrchestrationService(
        _cfg(tmp_path), repo, _FakeStore({"cookies": []}), notifier
    )

    with pytest.raises(OSError, match="backup disk unavailable"):
        await svc.run(42, AiBackupService.CLAUDE)

    assert repo.calls[0][0] == "failure"
    assert repo.calls[0][1] == AiBackupErrorCategory.NETWORK
    assert notifier.events == ["failure"]


async def test_manifest_finalize_error_does_not_record_success(tmp_path, monkeypatch) -> None:
    from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter

    def _raise_finalize(self, *_args, **_kwargs):
        raise OSError("manifest write failed")

    monkeypatch.setattr(AiBackupDiskWriter, "finalize_manifest", _raise_finalize)
    repo = _FakeRepo()
    notifier = _RecordingNotifier()
    _patch_browser_layer(monkeypatch, _OkClient())
    svc = AiBackupOrchestrationService(
        _cfg(tmp_path), repo, _FakeStore({"cookies": []}), notifier
    )

    with pytest.raises(OSError, match="manifest write failed"):
        await svc.run(42, AiBackupService.CLAUDE)

    assert [call[0] for call in repo.calls] == ["failure"]
    assert notifier.events == ["start", "failure"]
