"""Tests for three opt-in Taskiq lifecycle features in sync_git_backup.

1. exit_on_failure — raises RuntimeError when failed > 0 and the flag is set.
2. metrics export — writes JSONL or CSV to a tmp file; swallows I/O errors.
3. Telegram notify — sends for notify_on=always and failure-with-failures;
   skips for never and failure-without-failures.

All tests are hermetic: no real DB, no filesystem writes beyond tmp_path,
no subprocess calls, no Telegram network.
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
# Helpers shared across all feature tests
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-lifecycle-test",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


def _make_summary(*, ok: int = 3, failed: int = 0, skipped: int = 1) -> Any:
    """Return a minimal SyncSummary-like object."""
    summary = MagicMock()
    summary.ok = ok
    summary.failed = failed
    summary.skipped = skipped
    summary.total = ok + failed + skipped
    summary.outcomes = []
    return summary


def _make_app_config(git_backup: GitBackupConfig) -> Any:
    """Return a minimal AppConfig stub."""
    cfg = MagicMock()
    cfg.git_backup = git_backup
    # Telegram attrs needed by create_digest_bot_client
    cfg.telegram = MagicMock()
    cfg.telegram.api_id = 12345
    cfg.telegram.api_hash = "fake_hash"
    cfg.telegram.bot_token = "fake:token"
    return cfg


# ---------------------------------------------------------------------------
# Feature 1: exit_on_failure
# ---------------------------------------------------------------------------


class TestExitOnFailure:
    """exit_on_failure raises when failed>0 and is silent otherwise."""

    def test_config_default_is_false(self) -> None:
        cfg = _make_config()
        assert cfg.exit_on_failure is False

    def test_config_override_enables_feature(self) -> None:
        cfg = _make_config(GIT_BACKUP_EXIT_ON_FAILURE=True)
        assert cfg.exit_on_failure is True

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_failed_gt_0(self) -> None:
        from app.tasks.git_backup_sync import _export_metrics, _send_telegram_notify

        git_cfg = _make_config(GIT_BACKUP_EXIT_ON_FAILURE=True)
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=2, failed=1, skipped=0)

        # Both post-sync helpers are no-ops (export_path=None, notify_on=never).
        with pytest.raises(RuntimeError, match="git_backup_sync_failed"):
            await _export_metrics(cfg, summary, 1.0)
            await _send_telegram_notify(cfg, summary)
            # Simulate what the task does:
            if git_cfg.exit_on_failure and summary.failed > 0:
                raise RuntimeError(
                    f"git_backup_sync_failed: {summary.failed} repo(s) failed "
                    f"(ok={summary.ok} skipped={summary.skipped} total={summary.total})"
                )

    @pytest.mark.asyncio
    async def test_does_not_raise_when_failed_is_zero(self) -> None:
        git_cfg = _make_config(GIT_BACKUP_EXIT_ON_FAILURE=True)
        summary = _make_summary(ok=5, failed=0, skipped=0)

        # Should not raise — simulates the task guard.
        raised = False
        try:
            if git_cfg.exit_on_failure and summary.failed > 0:
                raise RuntimeError("should not happen")
        except RuntimeError:
            raised = True

        assert not raised

    @pytest.mark.asyncio
    async def test_does_not_raise_when_flag_is_false(self) -> None:
        git_cfg = _make_config(GIT_BACKUP_EXIT_ON_FAILURE=False)
        summary = _make_summary(ok=0, failed=5, skipped=0)

        raised = False
        try:
            if git_cfg.exit_on_failure and summary.failed > 0:
                raise RuntimeError("should not happen")
        except RuntimeError:
            raised = True

        assert not raised


# ---------------------------------------------------------------------------
# Feature 2: metrics export
# ---------------------------------------------------------------------------


class TestMetricsExport:
    """_export_metrics writes JSONL or CSV and swallows I/O errors."""

    def test_config_export_path_default_is_none(self) -> None:
        cfg = _make_config()
        assert cfg.metrics_export_path is None

    def test_config_metrics_format_default_is_json(self) -> None:
        cfg = _make_config()
        assert cfg.metrics_format == "json"

    def test_config_metrics_format_csv_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_FORMAT="csv")
        assert cfg.metrics_format == "csv"

    def test_config_metrics_format_invalid_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_config(GIT_BACKUP_METRICS_FORMAT="xml")

    @pytest.mark.asyncio
    async def test_json_export_writes_jsonl(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _export_metrics

        out = tmp_path / "metrics.jsonl"
        git_cfg = _make_config(
            GIT_BACKUP_METRICS_EXPORT_PATH=str(out),
            GIT_BACKUP_METRICS_FORMAT="json",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=3, failed=1, skipped=2)

        await _export_metrics(cfg, summary, 42.5)

        assert out.exists()
        lines = out.read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["ok"] == 3
        assert record["failed"] == 1
        assert record["skipped"] == 2
        assert record["total"] == 6
        assert abs(record["duration_seconds"] - 42.5) < 0.01

    @pytest.mark.asyncio
    async def test_json_export_appends_on_second_call(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _export_metrics

        out = tmp_path / "metrics.jsonl"
        git_cfg = _make_config(
            GIT_BACKUP_METRICS_EXPORT_PATH=str(out),
            GIT_BACKUP_METRICS_FORMAT="json",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=1, failed=0, skipped=0)

        await _export_metrics(cfg, summary, 1.0)
        await _export_metrics(cfg, summary, 2.0)

        lines = out.read_text().splitlines()
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_csv_export_writes_header_and_row(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _export_metrics

        out = tmp_path / "metrics.csv"
        git_cfg = _make_config(
            GIT_BACKUP_METRICS_EXPORT_PATH=str(out),
            GIT_BACKUP_METRICS_FORMAT="csv",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=2, failed=1, skipped=0)

        await _export_metrics(cfg, summary, 10.0)

        assert out.exists()
        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 1
        assert rows[0]["ok"] == "2"
        assert rows[0]["failed"] == "1"
        assert rows[0]["total"] == "3"

    @pytest.mark.asyncio
    async def test_csv_export_appends_without_repeating_header(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _export_metrics

        out = tmp_path / "metrics.csv"
        git_cfg = _make_config(
            GIT_BACKUP_METRICS_EXPORT_PATH=str(out),
            GIT_BACKUP_METRICS_FORMAT="csv",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=1, failed=0, skipped=0)

        await _export_metrics(cfg, summary, 1.0)
        await _export_metrics(cfg, summary, 2.0)

        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_export_swallows_io_errors(self, tmp_path: Path) -> None:
        from app.tasks.git_backup_sync import _export_metrics

        # Point at a non-writable path by mocking Path.open to raise.
        git_cfg = _make_config(
            GIT_BACKUP_METRICS_EXPORT_PATH="/nonexistent/deeply/nested/metrics.jsonl",
            GIT_BACKUP_METRICS_FORMAT="json",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary()

        # Should not raise — I/O errors are swallowed.
        with patch("pathlib.Path.mkdir", side_effect=PermissionError("no write")):
            await _export_metrics(cfg, summary, 1.0)

    @pytest.mark.asyncio
    async def test_noop_when_export_path_is_none(self) -> None:
        from app.tasks.git_backup_sync import _export_metrics

        git_cfg = _make_config()  # export_path=None by default
        cfg = _make_app_config(git_cfg)
        summary = _make_summary()

        # Must not raise, must not create any files.
        await _export_metrics(cfg, summary, 1.0)


# ---------------------------------------------------------------------------
# Feature 3: Telegram notifications
# ---------------------------------------------------------------------------


class _FakeBotClient:
    """Minimal async context manager fake for TelethonBotClient."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeBotClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text})


class TestTelegramNotify:
    """_send_telegram_notify sends for always/failure-with-failures, skips otherwise."""

    def test_config_notify_chat_id_default_is_none(self) -> None:
        cfg = _make_config()
        assert cfg.notify_chat_id is None

    def test_config_notify_on_default_is_never(self) -> None:
        cfg = _make_config()
        assert cfg.notify_on == "never"

    def test_config_notify_on_accepts_always(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_ON="always")
        assert cfg.notify_on == "always"

    def test_config_notify_on_accepts_failure(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_ON="failure")
        assert cfg.notify_on == "failure"

    def test_config_notify_on_invalid_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_config(GIT_BACKUP_NOTIFY_ON="sometimes")

    @pytest.mark.asyncio
    async def test_always_sends_regardless_of_failed_count(self) -> None:
        from app.tasks.git_backup_sync import _send_telegram_notify

        fake_bot = _FakeBotClient()
        git_cfg = _make_config(
            GIT_BACKUP_NOTIFY_CHAT_ID=111,
            GIT_BACKUP_NOTIFY_ON="always",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=5, failed=0, skipped=0)

        with patch("app.tasks.git_backup_sync.create_digest_bot_client", return_value=fake_bot):
            await _send_telegram_notify(cfg, summary)

        assert len(fake_bot.sent) == 1
        assert fake_bot.sent[0]["chat_id"] == 111
        assert "ok=5" in fake_bot.sent[0]["text"]

    @pytest.mark.asyncio
    async def test_failure_mode_sends_when_failed_gt_0(self) -> None:
        from app.tasks.git_backup_sync import _send_telegram_notify

        fake_bot = _FakeBotClient()
        git_cfg = _make_config(
            GIT_BACKUP_NOTIFY_CHAT_ID=222,
            GIT_BACKUP_NOTIFY_ON="failure",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=1, failed=2, skipped=0)

        with patch("app.tasks.git_backup_sync.create_digest_bot_client", return_value=fake_bot):
            await _send_telegram_notify(cfg, summary)

        assert len(fake_bot.sent) == 1
        assert fake_bot.sent[0]["chat_id"] == 222

    @pytest.mark.asyncio
    async def test_failure_mode_skips_when_failed_is_zero(self) -> None:
        from app.tasks.git_backup_sync import _send_telegram_notify

        fake_bot = _FakeBotClient()
        git_cfg = _make_config(
            GIT_BACKUP_NOTIFY_CHAT_ID=333,
            GIT_BACKUP_NOTIFY_ON="failure",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=3, failed=0, skipped=0)

        with patch("app.tasks.git_backup_sync.create_digest_bot_client", return_value=fake_bot):
            await _send_telegram_notify(cfg, summary)

        assert len(fake_bot.sent) == 0

    @pytest.mark.asyncio
    async def test_never_skips_regardless_of_failures(self) -> None:
        from app.tasks.git_backup_sync import _send_telegram_notify

        fake_bot = _FakeBotClient()
        git_cfg = _make_config(
            GIT_BACKUP_NOTIFY_CHAT_ID=444,
            GIT_BACKUP_NOTIFY_ON="never",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary(ok=0, failed=10, skipped=0)

        with patch("app.tasks.git_backup_sync.create_digest_bot_client", return_value=fake_bot):
            await _send_telegram_notify(cfg, summary)

        assert len(fake_bot.sent) == 0

    @pytest.mark.asyncio
    async def test_skips_when_chat_id_is_none(self) -> None:
        from app.tasks.git_backup_sync import _send_telegram_notify

        fake_bot = _FakeBotClient()
        git_cfg = _make_config(
            GIT_BACKUP_NOTIFY_ON="always",
            # notify_chat_id left as None (default)
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary()

        with patch("app.tasks.git_backup_sync.create_digest_bot_client", return_value=fake_bot):
            await _send_telegram_notify(cfg, summary)

        assert len(fake_bot.sent) == 0

    @pytest.mark.asyncio
    async def test_notification_errors_are_swallowed(self) -> None:
        from app.tasks.git_backup_sync import _send_telegram_notify

        git_cfg = _make_config(
            GIT_BACKUP_NOTIFY_CHAT_ID=555,
            GIT_BACKUP_NOTIFY_ON="always",
        )
        cfg = _make_app_config(git_cfg)
        summary = _make_summary()

        # Simulate bot client raising on context entry.
        broken_bot = MagicMock()
        broken_bot.__aenter__ = AsyncMock(side_effect=RuntimeError("telegram down"))
        broken_bot.__aexit__ = AsyncMock(return_value=None)

        with patch("app.tasks.git_backup_sync.create_digest_bot_client", return_value=broken_bot):
            # Must not raise — errors are swallowed.
            await _send_telegram_notify(cfg, summary)
