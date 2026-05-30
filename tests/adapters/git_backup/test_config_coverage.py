"""Targeted tests for uncovered branches in app.config.git_backup.

Covers the validator branches that were not reached by the existing suite:
- _validate_metrics_export_path: strip-whitespace-to-None and None input
- _validate_metrics_format: invalid value raises ValueError
- _validate_notify_chat_id: non-integer value raises ValueError
- _validate_ssl_ca_info: strip-whitespace-to-None branch
- _validate_http_version: invalid value raises ValueError
- _validate_hc_ping_url: strip-whitespace-to-None branch
- _validate_sync_cron: invalid cron expression raises ValueError
- _validate_maintenance_strategy: invalid value raises ValueError
- _validate_full_repack_interval: invalid value raises ValueError

All tests are hermetic: no DB, no filesystem I/O, no network.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.git_backup import GitBackupConfig


def _make_config(**overrides: object) -> GitBackupConfig:
    base: dict[str, object] = {"GIT_BACKUP_ENABLED": False}
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


# ---------------------------------------------------------------------------
# _validate_metrics_export_path
# ---------------------------------------------------------------------------


class TestMetricsExportPath:
    def test_none_input_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_EXPORT_PATH=None)
        assert cfg.metrics_export_path is None

    def test_empty_string_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_EXPORT_PATH="")
        assert cfg.metrics_export_path is None

    def test_whitespace_only_returns_none(self) -> None:
        # strip() reduces to "" which is falsy -> returns None (line 533 branch)
        cfg = _make_config(GIT_BACKUP_METRICS_EXPORT_PATH="   ")
        assert cfg.metrics_export_path is None

    def test_valid_path_returned_stripped(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_EXPORT_PATH="  /tmp/metrics.jsonl  ")
        assert cfg.metrics_export_path == "/tmp/metrics.jsonl"

    def test_default_is_none(self) -> None:
        cfg = _make_config()
        assert cfg.metrics_export_path is None


# ---------------------------------------------------------------------------
# _validate_metrics_format
# ---------------------------------------------------------------------------


class TestMetricsFormat:
    def test_none_falls_back_to_json(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_FORMAT=None)
        assert cfg.metrics_format == "json"

    def test_empty_string_falls_back_to_json(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_FORMAT="")
        assert cfg.metrics_format == "json"

    def test_json_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_FORMAT="json")
        assert cfg.metrics_format == "json"

    def test_csv_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_FORMAT="csv")
        assert cfg.metrics_format == "csv"

    def test_case_insensitive(self) -> None:
        cfg = _make_config(GIT_BACKUP_METRICS_FORMAT="JSON")
        assert cfg.metrics_format == "json"

    def test_invalid_value_raises(self) -> None:
        # Lines 551, 554-556: the fmt-not-in-allowed branch
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_METRICS_FORMAT="parquet")
        assert "GIT_BACKUP_METRICS_FORMAT" in str(exc_info.value)

    def test_invalid_value_error_mentions_allowed(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_METRICS_FORMAT="xml")
        err = str(exc_info.value)
        assert "csv" in err or "json" in err


# ---------------------------------------------------------------------------
# _validate_notify_chat_id
# ---------------------------------------------------------------------------


class TestNotifyChatId:
    def test_none_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_CHAT_ID=None)
        assert cfg.notify_chat_id is None

    def test_empty_string_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_CHAT_ID="")
        assert cfg.notify_chat_id is None

    def test_integer_string_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_CHAT_ID="123456789")
        assert cfg.notify_chat_id == 123456789

    def test_negative_integer_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_CHAT_ID="-100123456789")
        assert cfg.notify_chat_id == -100123456789

    def test_non_integer_string_raises(self) -> None:
        # Lines 554-556: the TypeError/ValueError except branch
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_NOTIFY_CHAT_ID="not-an-int")
        assert "GIT_BACKUP_NOTIFY_CHAT_ID" in str(exc_info.value)

    def test_float_string_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(GIT_BACKUP_NOTIFY_CHAT_ID="1.5")

    def test_default_is_none(self) -> None:
        cfg = _make_config()
        assert cfg.notify_chat_id is None


# ---------------------------------------------------------------------------
# _validate_notify_on (line 562 - invalid value branch)
# ---------------------------------------------------------------------------


class TestNotifyOn:
    def test_none_falls_back_to_never(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_ON=None)
        assert cfg.notify_on == "never"

    def test_empty_string_falls_back_to_never(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_ON="")
        assert cfg.notify_on == "never"

    def test_always_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_ON="always")
        assert cfg.notify_on == "always"

    def test_failure_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_NOTIFY_ON="failure")
        assert cfg.notify_on == "failure"

    def test_invalid_value_raises(self) -> None:
        # Line 562: the mode-not-in-allowed raise branch
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_NOTIFY_ON="daily")
        assert "GIT_BACKUP_NOTIFY_ON" in str(exc_info.value)

    def test_invalid_value_error_mentions_allowed(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_NOTIFY_ON="sometimes")
        err = str(exc_info.value)
        assert "always" in err or "never" in err or "failure" in err


# ---------------------------------------------------------------------------
# _validate_ssl_ca_info (lines 592-594: whitespace-only -> None)
# ---------------------------------------------------------------------------


class TestSslCaInfo:
    def test_none_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_SSL_CA_INFO=None)
        assert cfg.ssl_ca_info is None

    def test_empty_string_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_SSL_CA_INFO="")
        assert cfg.ssl_ca_info is None

    def test_whitespace_only_returns_none(self) -> None:
        # Lines 592-594: strip() -> "" -> falsy -> None
        cfg = _make_config(GIT_BACKUP_SSL_CA_INFO="   ")
        assert cfg.ssl_ca_info is None

    def test_valid_path_returned(self) -> None:
        cfg = _make_config(GIT_BACKUP_SSL_CA_INFO="/etc/ssl/ca-bundle.pem")
        assert cfg.ssl_ca_info == "/etc/ssl/ca-bundle.pem"

    def test_valid_path_stripped(self) -> None:
        cfg = _make_config(GIT_BACKUP_SSL_CA_INFO="  /etc/ssl/ca.pem  ")
        assert cfg.ssl_ca_info == "/etc/ssl/ca.pem"


# ---------------------------------------------------------------------------
# _validate_http_version (lines 599-605: invalid value raise)
# ---------------------------------------------------------------------------


class TestHttpVersion:
    def test_none_falls_back_to_http1(self) -> None:
        cfg = _make_config(GIT_BACKUP_HTTP_VERSION=None)
        assert cfg.http_version == "HTTP/1.1"

    def test_empty_string_falls_back_to_http1(self) -> None:
        cfg = _make_config(GIT_BACKUP_HTTP_VERSION="")
        assert cfg.http_version == "HTTP/1.1"

    def test_http1_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_HTTP_VERSION="HTTP/1.1")
        assert cfg.http_version == "HTTP/1.1"

    def test_http2_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_HTTP_VERSION="HTTP/2")
        assert cfg.http_version == "HTTP/2"

    def test_invalid_value_raises(self) -> None:
        # Lines 599-605: version-not-in-allowed raise branch
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_HTTP_VERSION="HTTP/3")
        assert "GIT_BACKUP_HTTP_VERSION" in str(exc_info.value)

    def test_invalid_value_error_mentions_allowed(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_HTTP_VERSION="HTTPS/1.1")
        err = str(exc_info.value)
        assert "HTTP/1.1" in err or "HTTP/2" in err


# ---------------------------------------------------------------------------
# _validate_hc_ping_url (line 611: whitespace-only -> None)
# ---------------------------------------------------------------------------


class TestHcPingUrl:
    def test_none_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_HC_PING_URL=None)
        assert cfg.hc_ping_url is None

    def test_empty_string_returns_none(self) -> None:
        cfg = _make_config(GIT_BACKUP_HC_PING_URL="")
        assert cfg.hc_ping_url is None

    def test_whitespace_only_returns_none(self) -> None:
        # Line 611: strip() -> "" -> falsy -> None
        cfg = _make_config(GIT_BACKUP_HC_PING_URL="   ")
        assert cfg.hc_ping_url is None

    def test_valid_url_returned(self) -> None:
        cfg = _make_config(GIT_BACKUP_HC_PING_URL="https://hc-ping.com/abc-123")
        assert cfg.hc_ping_url == "https://hc-ping.com/abc-123"

    def test_valid_url_stripped(self) -> None:
        cfg = _make_config(GIT_BACKUP_HC_PING_URL="  https://hc-ping.com/uuid  ")
        assert cfg.hc_ping_url == "https://hc-ping.com/uuid"


# ---------------------------------------------------------------------------
# _validate_sync_cron (lines 615-616: invalid cron raises)
# ---------------------------------------------------------------------------


class TestSyncCron:
    def test_none_falls_back_to_default(self) -> None:
        cfg = _make_config(GIT_BACKUP_SYNC_CRON=None)
        assert cfg.sync_cron == "0 4 * * *"

    def test_empty_string_falls_back_to_default(self) -> None:
        cfg = _make_config(GIT_BACKUP_SYNC_CRON="")
        assert cfg.sync_cron == "0 4 * * *"

    def test_valid_cron_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_SYNC_CRON="30 2 * * 0")
        assert cfg.sync_cron == "30 2 * * 0"

    def test_four_field_cron_raises(self) -> None:
        # Lines 615-616: len(split()) != 5 raise branch
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_SYNC_CRON="0 4 * *")
        assert "GIT_BACKUP_SYNC_CRON" in str(exc_info.value)

    def test_six_field_cron_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_SYNC_CRON="0 4 * * * extra")
        assert "GIT_BACKUP_SYNC_CRON" in str(exc_info.value)

    def test_whitespace_only_cron_raises(self) -> None:
        # "   ".strip() -> "" -> split() -> [] -> len != 5 -> raises
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_SYNC_CRON="   ")
        assert "GIT_BACKUP_SYNC_CRON" in str(exc_info.value)

    def test_single_word_cron_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(GIT_BACKUP_SYNC_CRON="@daily")


# ---------------------------------------------------------------------------
# _validate_maintenance_strategy (lines 622-623: invalid value raises)
# ---------------------------------------------------------------------------


class TestMaintenanceStrategy:
    def test_none_falls_back_to_default(self) -> None:
        cfg = _make_config(GIT_BACKUP_MAINTENANCE_STRATEGY=None)
        assert cfg.maintenance_strategy == "gc-auto"

    def test_empty_string_falls_back_to_default(self) -> None:
        cfg = _make_config(GIT_BACKUP_MAINTENANCE_STRATEGY="")
        assert cfg.maintenance_strategy == "gc-auto"

    def test_gc_auto_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_MAINTENANCE_STRATEGY="gc-auto")
        assert cfg.maintenance_strategy == "gc-auto"

    def test_geometric_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_MAINTENANCE_STRATEGY="geometric")
        assert cfg.maintenance_strategy == "geometric"

    def test_none_strategy_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_MAINTENANCE_STRATEGY="none")
        assert cfg.maintenance_strategy == "none"

    def test_invalid_value_raises(self) -> None:
        # Lines 622-623: strategy-not-in-allowed raise branch
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_MAINTENANCE_STRATEGY="aggressive")
        assert "GIT_BACKUP_MAINTENANCE_STRATEGY" in str(exc_info.value)

    def test_invalid_value_error_mentions_allowed(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_MAINTENANCE_STRATEGY="full")
        err = str(exc_info.value)
        assert "gc-auto" in err or "geometric" in err or "none" in err


# ---------------------------------------------------------------------------
# _validate_full_repack_interval (lines 624-629: invalid value raises)
# ---------------------------------------------------------------------------


class TestFullRepackInterval:
    def test_none_falls_back_to_default(self) -> None:
        cfg = _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL=None)
        assert cfg.full_repack_interval == "never"

    def test_empty_string_falls_back_to_default(self) -> None:
        cfg = _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL="")
        assert cfg.full_repack_interval == "never"

    def test_never_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL="never")
        assert cfg.full_repack_interval == "never"

    def test_weekly_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL="weekly")
        assert cfg.full_repack_interval == "weekly"

    def test_monthly_accepted(self) -> None:
        cfg = _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL="monthly")
        assert cfg.full_repack_interval == "monthly"

    def test_invalid_value_raises(self) -> None:
        # Lines 624-629: interval-not-in-allowed raise branch
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL="daily")
        assert "GIT_BACKUP_FULL_REPACK_INTERVAL" in str(exc_info.value)

    def test_invalid_value_error_mentions_allowed(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL="yearly")
        err = str(exc_info.value)
        assert "weekly" in err or "monthly" in err or "never" in err

    def test_invalid_case_raises(self) -> None:
        # Validator does not lowercase -- "Weekly" is not in the allowed set
        with pytest.raises(ValidationError):
            _make_config(GIT_BACKUP_FULL_REPACK_INTERVAL="Weekly")
