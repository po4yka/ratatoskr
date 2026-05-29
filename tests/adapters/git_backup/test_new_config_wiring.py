"""Tests for the newly-wired gitout config fields in GitBackupConfig and build_git_command.

Covers:
- build_git_command emits the correct flags for verify_certificates, post_buffer_size,
  low_speed_limit / low_speed_time, single_branch_only, and use_shallow_clone.
- _should_use_shallow_clone selects shallow clones correctly based on
  consecutive_failures and size_kb thresholds.

All tests are hermetic: no DB, no filesystem, no subprocesses.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.adapters.git_backup.git_commands import build_git_command
from app.adapters.git_backup.mirror_service import _should_use_shallow_clone
from app.config.git_backup import GitBackupConfig
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URL = "https://github.com/octocat/hello-world.git"
_REPO_NAME = "octocat/hello-world"


def _make_mirror(
    *,
    consecutive_failures: int = 0,
    size_kb: int | None = None,
) -> GitMirror:
    """Return a minimal GitMirror stub without touching the DB."""
    return GitMirror(
        id=1,
        user_id=100,
        source=GitMirrorSource.GITHUB,
        clone_url=_URL,
        consecutive_failures=consecutive_failures,
        size_kb=size_kb,
        status=GitMirrorStatus.PENDING,
    )


def _make_config(**overrides: object) -> GitBackupConfig:
    """Return a GitBackupConfig with sensible defaults and selected overrides."""
    base = {
        "GIT_BACKUP_ENABLED": False,
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


# ---------------------------------------------------------------------------
# build_git_command flag tests
# ---------------------------------------------------------------------------


class TestBuildGitCommandFlags:
    """Assert that build_git_command emits the right -c flags for each new param."""

    def test_ssl_verify_disabled_emits_ssl_verify_false(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            verify_certificates=False,
        )
        assert "-c" in argv
        assert "http.sslVerify=false" in argv

    def test_ssl_verify_enabled_no_ssl_verify_flag(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            verify_certificates=True,
        )
        assert "http.sslVerify=false" not in argv

    def test_custom_post_buffer_size(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            post_buffer_size=1_048_576,
        )
        assert "http.postBuffer=1048576" in argv

    def test_default_post_buffer_size(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
        )
        assert "http.postBuffer=524288000" in argv

    def test_low_speed_flags_present_when_limit_nonzero(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            low_speed_limit=500,
            low_speed_time=30,
        )
        assert "http.lowSpeedLimit=500" in argv
        assert "http.lowSpeedTime=30" in argv

    def test_low_speed_flags_absent_when_limit_zero(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            low_speed_limit=0,
        )
        # Neither lowSpeedLimit nor lowSpeedTime should appear.
        assert not any("lowSpeed" in token for token in argv)

    def test_single_branch_only_clone_emits_single_branch_flag(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            single_branch_only=True,
        )
        assert "--single-branch" in argv
        assert "--bare" in argv

    def test_shallow_clone_emits_depth_1_and_single_branch(self) -> None:
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            use_shallow_clone=True,
        )
        assert "--depth=1" in argv
        assert "--single-branch" in argv

    def test_shallow_clone_ignored_on_update(self) -> None:
        """use_shallow_clone has no effect when the repo already exists."""
        argv = build_git_command(
            repo_exists=True,
            use_shallow_clone=True,
        )
        assert "--depth=1" not in argv
        assert "remote" in argv  # update path

    def test_ssl_verify_false_appears_before_http_version(self) -> None:
        """http.sslVerify=false must come before http.version=HTTP/1.1 in argv."""
        argv = build_git_command(
            repo_exists=False,
            url=_URL,
            repo_name=_REPO_NAME,
            verify_certificates=False,
        )
        ssl_idx = next(i for i, t in enumerate(argv) if t == "http.sslVerify=false")
        ver_idx = next((i for i, t in enumerate(argv) if t == "http.version=HTTP/1.1"), None)
        if ver_idx is not None:
            assert ssl_idx < ver_idx


# ---------------------------------------------------------------------------
# _should_use_shallow_clone logic tests
# ---------------------------------------------------------------------------


class TestShouldUseShallowClone:
    """Unit tests for the shallow-clone selection helper."""

    def test_both_disabled_never_shallow(self) -> None:
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=0,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=0,
        )
        mirror = _make_mirror(consecutive_failures=100, size_kb=9_000_000)
        assert _should_use_shallow_clone(mirror, cfg) is False

    def test_failure_threshold_only_met(self) -> None:
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=0,
        )
        mirror = _make_mirror(consecutive_failures=3)
        assert _should_use_shallow_clone(mirror, cfg) is True

    def test_failure_threshold_only_not_met(self) -> None:
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=0,
        )
        mirror = _make_mirror(consecutive_failures=2)
        assert _should_use_shallow_clone(mirror, cfg) is False

    def test_size_threshold_only_met(self) -> None:
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=0,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=2_000_000,
        )
        mirror = _make_mirror(size_kb=2_000_000)
        assert _should_use_shallow_clone(mirror, cfg) is True

    def test_size_threshold_only_not_met(self) -> None:
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=0,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=2_000_000,
        )
        mirror = _make_mirror(size_kb=1_999_999)
        assert _should_use_shallow_clone(mirror, cfg) is False

    def test_size_threshold_only_size_unknown(self) -> None:
        """When size_kb is None and size threshold is set, should not shallow-clone."""
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=0,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=2_000_000,
        )
        mirror = _make_mirror(size_kb=None)
        assert _should_use_shallow_clone(mirror, cfg) is False

    def test_both_thresholds_both_met(self) -> None:
        """gitout AND semantics: both conditions must be met."""
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=2_000_000,
        )
        mirror = _make_mirror(consecutive_failures=3, size_kb=2_000_000)
        assert _should_use_shallow_clone(mirror, cfg) is True

    def test_both_thresholds_only_failures_met(self) -> None:
        """When both conditions configured, size not met -> no shallow clone."""
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=2_000_000,
        )
        mirror = _make_mirror(consecutive_failures=5, size_kb=100_000)
        assert _should_use_shallow_clone(mirror, cfg) is False

    def test_both_thresholds_only_size_met(self) -> None:
        """When both conditions configured, failures not met -> no shallow clone."""
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=2_000_000,
        )
        mirror = _make_mirror(consecutive_failures=2, size_kb=3_000_000)
        assert _should_use_shallow_clone(mirror, cfg) is False

    def test_exactly_at_failure_threshold(self) -> None:
        """Boundary: consecutive_failures == threshold selects shallow."""
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=0,
        )
        mirror = _make_mirror(consecutive_failures=3)
        assert _should_use_shallow_clone(mirror, cfg) is True

    def test_one_below_failure_threshold(self) -> None:
        cfg = _make_config(
            GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3,
            GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=0,
        )
        mirror = _make_mirror(consecutive_failures=2)
        assert _should_use_shallow_clone(mirror, cfg) is False


# ---------------------------------------------------------------------------
# GitBackupConfig field defaults and validation
# ---------------------------------------------------------------------------


class TestGitBackupConfigNewFields:
    """Validate defaults and field aliases for the new config fields."""

    def test_defaults_match_gitout(self) -> None:
        cfg = _make_config()
        assert cfg.verify_certificates is True
        assert cfg.post_buffer_size == 524_288_000
        assert cfg.low_speed_limit == 1000
        assert cfg.low_speed_time == 60
        assert cfg.single_branch_only is False
        assert cfg.shallow_clone_threshold_kb == 0
        assert cfg.shallow_clone_after_failures == 0

    def test_verify_certificates_override(self) -> None:
        cfg = _make_config(GIT_BACKUP_VERIFY_CERTIFICATES=False)
        assert cfg.verify_certificates is False

    def test_post_buffer_size_override(self) -> None:
        cfg = _make_config(GIT_BACKUP_POST_BUFFER_SIZE=1_048_576)
        assert cfg.post_buffer_size == 1_048_576

    def test_low_speed_override(self) -> None:
        cfg = _make_config(GIT_BACKUP_LOW_SPEED_LIMIT=500, GIT_BACKUP_LOW_SPEED_TIME=30)
        assert cfg.low_speed_limit == 500
        assert cfg.low_speed_time == 30

    def test_single_branch_only_override(self) -> None:
        cfg = _make_config(GIT_BACKUP_SINGLE_BRANCH_ONLY=True)
        assert cfg.single_branch_only is True

    def test_shallow_clone_threshold_override(self) -> None:
        cfg = _make_config(GIT_BACKUP_SHALLOW_CLONE_THRESHOLD_KB=2_000_000)
        assert cfg.shallow_clone_threshold_kb == 2_000_000

    def test_shallow_clone_after_failures_override(self) -> None:
        cfg = _make_config(GIT_BACKUP_SHALLOW_CLONE_AFTER_FAILURES=3)
        assert cfg.shallow_clone_after_failures == 3

    def test_post_buffer_size_min_validation(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(GIT_BACKUP_POST_BUFFER_SIZE=512)  # below ge=1024

    def test_low_speed_time_min_validation(self) -> None:
        with pytest.raises(ValidationError):
            _make_config(GIT_BACKUP_LOW_SPEED_TIME=0)  # below ge=1
