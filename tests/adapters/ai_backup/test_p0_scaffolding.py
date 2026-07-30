"""P0 scaffolding tests for the AI account backup subsystem.

Covers the config validators, model registration, and the task's
service-selection helper. The authenticated-scrape behavior is added in later
phases and is not exercised here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config.ai_backup import AiBackupConfig
from app.db.models.ai_backup import (
    AiAccountBackup,
    AiBackupAuthorizationStatus,
    AiBackupService,
    AiBackupStatus,
)
from app.tasks.ai_backup_sync import _enabled_services


def test_config_defaults_disabled() -> None:
    cfg = AiBackupConfig()
    assert cfg.enabled is False
    assert cfg.chatgpt_enabled is False
    assert cfg.claude_enabled is False
    assert cfg.any_service_enabled is False
    assert cfg.sync_cron == "0 5 * * *"
    assert cfg.notify_on == "never"
    assert "claude.ai" in cfg.host_allowlist
    assert "chatgpt.com" in cfg.host_allowlist
    assert cfg.max_response_bytes < cfg.max_run_bytes
    assert cfg.min_free_bytes > 0
    assert cfg.browser_locale == "en-US"
    assert cfg.browser_timezone == "Asia/Tbilisi"


def test_any_service_enabled_toggles() -> None:
    assert AiBackupConfig(chatgpt_enabled=True).any_service_enabled is True
    assert AiBackupConfig(claude_enabled=True).any_service_enabled is True


def test_host_allowlist_accepts_comma_string() -> None:
    cfg = AiBackupConfig(host_allowlist="a.com, b.com ,c.com")
    assert cfg.host_allowlist == ["a.com", "b.com", "c.com"]


def test_host_allowlist_accepts_list() -> None:
    cfg = AiBackupConfig(host_allowlist=["x.com", " y.com "])
    assert cfg.host_allowlist == ["x.com", "y.com"]


def test_invalid_cron_rejected() -> None:
    with pytest.raises(ValidationError):
        AiBackupConfig(sync_cron="not-a-cron")


def test_relative_data_path_rejected() -> None:
    with pytest.raises(ValidationError):
        AiBackupConfig(data_path="relative/backups")


def test_absolute_data_path_accepted() -> None:
    assert AiBackupConfig(data_path="/srv/ai-backups").data_path == "/srv/ai-backups"


def test_reauth_targets_are_isolated_per_provider() -> None:
    cfg = AiBackupConfig()

    assert cfg.reauth_chatgpt_browser_url == "http://cloakbrowser-reauth-chatgpt:9222"
    assert cfg.reauth_claude_browser_url == "http://cloakbrowser-reauth-claude:9222"
    assert cfg.reauth_chatgpt_vnc_host == "ai-backup-display-chatgpt"
    assert cfg.reauth_claude_vnc_host == "ai-backup-display-claude"
    assert cfg.reauth_viewer_ticket_ttl_seconds == 60


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reauth_chatgpt_browser_url", "file:///tmp/browser"),
        ("reauth_claude_browser_url", ""),
        ("reauth_chatgpt_vnc_host", " "),
        ("reauth_claude_vnc_port", 0),
        ("reauth_viewer_ticket_ttl_seconds", 0),
        ("reauth_vnc_connect_timeout_seconds", 0),
    ],
)
def test_invalid_reauth_transport_configuration_fails_fast(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AiBackupConfig(**{field: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"browser_locale": "   "},
        {"browser_timezone": "Not/A_Timezone"},
    ],
)
def test_invalid_browser_profile_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        AiBackupConfig(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_response_bytes": 0},
        {"max_run_bytes": 0},
        {"min_free_bytes": -1},
    ],
)
def test_backup_byte_caps_reject_invalid_values(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        AiBackupConfig(**kwargs)


def test_invalid_notify_on_rejected() -> None:
    with pytest.raises(ValidationError):
        AiBackupConfig(notify_on="sometimes")


def test_hc_ping_url_scheme_guard() -> None:
    with pytest.raises(ValidationError):
        AiBackupConfig(hc_ping_url="ftp://example.com/ping")


def test_notify_chat_id_coerced_to_int() -> None:
    assert AiBackupConfig(notify_chat_id="123").notify_chat_id == 123


def test_model_registered_in_all_models() -> None:
    from app.db.models import ALL_MODELS

    assert AiAccountBackup in ALL_MODELS
    assert AiAccountBackup.__tablename__ == "ai_account_backups"


def test_enum_values_stable() -> None:
    assert {s.value for s in AiBackupService} == {"chatgpt", "claude"}
    assert {s.value for s in AiBackupStatus} == {
        "pending",
        "ok",
        "failed",
        "auth_expired",
        "disabled",
    }
    assert {s.value for s in AiBackupAuthorizationStatus} == {
        "missing",
        "unverified",
        "valid",
        "expired",
    }


def test_unique_constraint_on_user_service() -> None:
    constraints = {c.name for c in AiAccountBackup.__table__.constraints if c.name}
    assert "uq_ai_account_backups_user_service" in constraints


@pytest.mark.parametrize(
    ("chatgpt", "claude", "expected"),
    [
        (False, False, []),
        (True, False, [AiBackupService.CHATGPT]),
        (False, True, [AiBackupService.CLAUDE]),
        (True, True, [AiBackupService.CHATGPT, AiBackupService.CLAUDE]),
    ],
)
def test_enabled_services_helper(chatgpt: bool, claude: bool, expected: list) -> None:
    cfg = SimpleNamespace(ai_backup=AiBackupConfig(chatgpt_enabled=chatgpt, claude_enabled=claude))
    assert _enabled_services(cfg) == expected
