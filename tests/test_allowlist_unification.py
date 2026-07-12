"""Matrix test: ALLOWED_USER_IDS allowlist semantics across auth paths.

Locks the unified contract: when ALLOWED_USER_IDS is empty, every auth path
fails closed. The previous divergence — JWT (dependencies.py:119) used
fail_open_when_empty=True while WebApp / Telegram-Login / secret-login all
used False — was a security gap exposed when Settings(allow_stub_telegram=True)
bypassed the startup validator at app/config/settings.py:315.

The test below verifies two complementary properties:

1. Config.is_user_allowed defaults to fail-closed and returns False on an
   empty allowlist regardless of the user_id checked.
2. All four auth-path call sites pass fail_open_when_empty=False (or omit
   it, taking the safe default). Done by static text search rather than
   monkey-driving the four routes — the routes already have integration
   coverage in tests/api/test_telegram_linking.py, tests/api/test_secret_login.py,
   and tests/test_webapp_auth.py; what we lock here is the call-site shape.
"""

from __future__ import annotations

import os
import unittest.mock
from pathlib import Path

import pytest

from app.api.exceptions import AuthorizationError
from app.api.routers.auth import tokens
from app.config import Config
from tests._config_env import MODEL_SELECTION_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_PATHS = [
    "app/api/routers/auth/dependencies.py",
    "app/api/routers/auth/webapp_auth.py",
    "app/api/routers/auth/telegram.py",
    "app/api/routers/auth/secret_auth.py",
]


def test_is_user_allowed_fails_closed_when_allowlist_empty(monkeypatch):
    """Empty ALLOWED_USER_IDS + default fail_open → reject (fail-closed)."""
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    assert Config.is_user_allowed(123456789) is False


def test_is_user_allowed_admits_listed_user(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "123456789,987654321")
    assert Config.is_user_allowed(987654321) is True


def test_is_user_allowed_rejects_unlisted_user(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "123456789")
    assert Config.is_user_allowed(999999999) is False


def test_is_user_allowed_legacy_fail_open_path_still_works(monkeypatch):
    """fail_open_when_empty=True still admits when allowlist is empty.

    The flag stays in the API surface for tests / scripts that explicitly
    opt in. No production auth path uses it — the regression test below
    verifies that.
    """
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    assert Config.is_user_allowed(123456789, fail_open_when_empty=True) is True


@pytest.mark.parametrize("relative_path", AUTH_PATHS)
def test_no_auth_path_passes_fail_open_when_empty_true(relative_path: str):
    """Static guard: no auth path may opt into fail-open semantics.

    fail_open_when_empty=True at any of the four auth call sites would re-
    introduce the divergence this task closed. Easier to grep than to wire
    up four parameterised request mocks.
    """
    text = (REPO_ROOT / relative_path).read_text()
    assert "fail_open_when_empty=True" not in text, (
        f"{relative_path} reintroduces fail-open semantics — see "
        "docs/tasks/issues archive: unify-allowed-user-ids-allowlist-semantics"
    )


def test_config_helper_delegates_to_appconfig(monkeypatch):
    """Patching AppConfig.telegram.allowed_user_ids must propagate through
    Config.is_user_allowed — proving the helper reads the validated config
    object, not raw env vars. This is the contract that keeps tests honest
    about which deploys can authenticate which users.
    """
    from app.config import settings

    monkeypatch.setenv("ALLOWED_USER_IDS", "111,222")
    settings.clear_config_cache()
    assert settings.Config.get_allowed_user_ids() == (111, 222)
    assert settings.Config.is_user_allowed(111) is True
    assert settings.Config.is_user_allowed(333) is False

    monkeypatch.setenv("ALLOWED_USER_IDS", "555")
    settings.clear_config_cache()
    assert settings.Config.get_allowed_user_ids() == (555,)
    assert settings.Config.is_user_allowed(111) is False
    assert settings.Config.is_user_allowed(555) is True


def test_config_helper_get_allowed_client_ids_delegates_to_authconfig(monkeypatch):
    """ALLOWED_CLIENT_IDS now lives on AuthConfig.allowed_client_ids; the
    helper reads it through load_config() rather than os.getenv directly."""
    from app.config import settings

    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "android-app, ios-app, cli")
    settings.clear_config_cache()
    assert settings.Config.get_allowed_client_ids() == ("android-app", "ios-app", "cli")

    # Empty / unset → no restriction only for development/local posture.
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    settings.clear_config_cache()
    assert settings.Config.get_allowed_client_ids() == ()


def test_authconfig_drops_invalid_client_ids(monkeypatch):
    """The validator silently drops client ids with invalid characters or
    excessive length. Behavior preserved from the previous helper."""
    from app.config import settings
    from app.config.api import AuthConfig

    cfg = AuthConfig(allowed_client_ids="ok-1,bad space,also$bad,fine_id")
    assert cfg.allowed_client_ids == ("ok-1", "fine_id")

    too_long = "a" * 101
    cfg = AuthConfig(allowed_client_ids=f"keeper,{too_long}")
    assert cfg.allowed_client_ids == ("keeper",)

    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")
    settings.clear_config_cache()


_MINIMAL_SETTINGS_ENV = {
    # Model selection is required (no code default); supply it so the cleared
    # environment can still build Settings. See tests/_config_env.py.
    **MODEL_SELECTION_ENV,
    "API_ID": "12345",
    "API_HASH": "abc123",
    "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ALLOWED_USER_IDS": "999",
    "FIRECRAWL_API_KEY": "",
    "OPENROUTER_API_KEY": "sk-test",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "GITHUB_TOKEN_ENCRYPTION_KEY": "QpuAsYbqcPtUCkWXZzjYmmVgjV5QV0VTmUz2pZjWpEA=",
}


def test_production_empty_client_allowlist_fails_startup():
    from app.config import settings

    with unittest.mock.patch.dict(
        os.environ,
        {
            **_MINIMAL_SETTINGS_ENV,
            "APP_ENV": "production",
            "REDIS_ENABLED": "true",
            "REDIS_REQUIRED": "true",
            # ratatoskr.yaml pins redis.required=false (YAML beats env), which
            # would trip the rate-limit gate before the allowlist gate the
            # test wants to assert on. Point the loader at a missing path so
            # the env-set REDIS_REQUIRED=true is honored.
            "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
            "ALLOWED_CLIENT_IDS": "",
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with pytest.raises(RuntimeError, match="Production deployment requires ALLOWED_CLIENT_IDS"):
            settings.Settings(allow_stub_telegram=True)


def test_production_empty_client_allowlist_with_explicit_override_starts_with_warning():
    from app.config import settings

    with unittest.mock.patch.dict(
        os.environ,
        {
            **_MINIMAL_SETTINGS_ENV,
            "APP_ENV": "production",
            "REDIS_ENABLED": "true",
            "REDIS_REQUIRED": "true",
            # See sibling test: avoid checked-in YAML overriding the env-set
            # REDIS_REQUIRED=true so the allowlist-warning path is exercised.
            "RATATOSKR_CONFIG": "/nonexistent/ratatoskr.yaml",
            "ALLOWED_CLIENT_IDS": "",
            "AUTH_ALLOW_ANY_CLIENT_ID": "true",
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with unittest.mock.patch.object(settings.logger, "warning") as warning:
            cfg = settings.Settings(allow_stub_telegram=True)

    assert cfg.auth.allow_any_client_id is True
    warning.assert_any_call(
        "auth_allow_any_client_id_override_active",
        extra={
            "app_env": "production",
            "api_public_exposure": False,
            "warning": (
                "AUTH_ALLOW_ANY_CLIENT_ID=true: every syntactically valid "
                "client_id can authenticate while ALLOWED_CLIENT_IDS is empty."
            ),
        },
    )


def test_development_empty_client_allowlist_starts_with_warning():
    from app.config import settings

    with unittest.mock.patch.dict(
        os.environ,
        {
            **_MINIMAL_SETTINGS_ENV,
            "APP_ENV": "development",
            "ALLOWED_CLIENT_IDS": "",
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with unittest.mock.patch.object(settings.logger, "warning") as warning:
            cfg = settings.Settings(allow_stub_telegram=True)

    assert cfg.deployment.is_production_mode is False
    assert cfg.auth.allowed_client_ids == ()
    warning.assert_any_call(
        "auth_client_allowlist_empty_development",
        extra={
            "app_env": "development",
            "api_public_exposure": False,
            "warning": (
                "ALLOWED_CLIENT_IDS is empty; every syntactically valid client_id "
                "is accepted. This is intended only for local/development use."
            ),
        },
    )


def test_unknown_client_id_rejected_when_allowlist_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "web-v1,cli-v1")
    settings.clear_config_cache()

    with pytest.raises(AuthorizationError):
        tokens.validate_client_id("mobile-v1")


def test_known_client_id_accepted_when_allowlist_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "web-v1,cli-v1")
    settings.clear_config_cache()

    tokens.validate_client_id("web-v1")


_KNOWN_CLIENT_IDS_WARNING = (
    "ALLOWED_CLIENT_IDS omits official client_ids listed in "
    "KNOWN_CLIENT_IDS; those clients will be rejected at auth. "
    "Add them to ALLOWED_CLIENT_IDS, or set "
    "AUTH_ALLOW_ANY_CLIENT_ID=true if the omission is intentional."
)


def _warning_calls(warning_mock, event: str):
    """Return every logger.warning call whose first positional arg is ``event``."""
    return [c for c in warning_mock.call_args_list if c.args and c.args[0] == event]


def test_explicit_allowlist_omitting_known_client_id_warns_at_startup():
    """A non-empty ALLOWED_CLIENT_IDS that drops a KNOWN_CLIENT_IDS entry must
    emit a startup warning naming exactly the missing official ids."""
    from app.config import settings
    from app.config.known_client_ids import KNOWN_CLIENT_IDS

    known_sorted = sorted(KNOWN_CLIENT_IDS)
    omitted = known_sorted[0]
    allowed = known_sorted[1:]  # every known id except the first

    with unittest.mock.patch.dict(
        os.environ,
        {
            **_MINIMAL_SETTINGS_ENV,
            "APP_ENV": "development",
            "ALLOWED_CLIENT_IDS": ",".join(allowed),
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with unittest.mock.patch.object(settings.logger, "warning") as warning:
            settings.Settings(allow_stub_telegram=True)

    warning.assert_any_call(
        "auth_known_client_ids_not_allowlisted",
        extra={
            "app_env": "development",
            "missing_known_client_ids": [omitted],
            "warning": _KNOWN_CLIENT_IDS_WARNING,
        },
    )


def test_allowlist_superset_of_known_client_ids_does_not_warn():
    """When ALLOWED_CLIENT_IDS is a superset of KNOWN_CLIENT_IDS, the cross-check
    stays silent (extra custom ids are allowed)."""
    from app.config import settings
    from app.config.known_client_ids import KNOWN_CLIENT_IDS

    allowed = [*sorted(KNOWN_CLIENT_IDS), "extra-custom-client"]

    with unittest.mock.patch.dict(
        os.environ,
        {
            **_MINIMAL_SETTINGS_ENV,
            "APP_ENV": "development",
            "ALLOWED_CLIENT_IDS": ",".join(allowed),
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with unittest.mock.patch.object(settings.logger, "warning") as warning:
            settings.Settings(allow_stub_telegram=True)

    assert _warning_calls(warning, "auth_known_client_ids_not_allowlisted") == []


def test_allow_any_client_id_suppresses_known_client_id_cross_check():
    """AUTH_ALLOW_ANY_CLIENT_ID=true accepts every valid client_id, so the
    known-ids cross-check must not fire even when the allowlist omits them."""
    from app.config import settings

    with unittest.mock.patch.dict(
        os.environ,
        {
            **_MINIMAL_SETTINGS_ENV,
            "APP_ENV": "development",
            "ALLOWED_CLIENT_IDS": "custom-only",
            "AUTH_ALLOW_ANY_CLIENT_ID": "true",
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with unittest.mock.patch.object(settings.logger, "warning") as warning:
            settings.Settings(allow_stub_telegram=True)

    assert _warning_calls(warning, "auth_known_client_ids_not_allowlisted") == []


def test_empty_allowlist_skips_known_client_id_cross_check():
    """An empty allowlist is fail-open (handled by the empty-allowlist validator);
    the known-ids cross-check is meaningful only for an explicit allowlist and
    must not fire when none is set."""
    from app.config import settings

    with unittest.mock.patch.dict(
        os.environ,
        {
            **_MINIMAL_SETTINGS_ENV,
            "APP_ENV": "development",
            "ALLOWED_CLIENT_IDS": "",
        },
        clear=True,
    ):
        settings.clear_config_cache()
        with unittest.mock.patch.object(settings.logger, "warning") as warning:
            settings.Settings(allow_stub_telegram=True)

    assert _warning_calls(warning, "auth_known_client_ids_not_allowlisted") == []


def test_auth_posture_summary_is_redacted_counts_only(monkeypatch):
    from app.config import settings

    monkeypatch.setenv("ALLOWED_USER_IDS", "111,222")
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "web-v1,cli-v1")
    settings.clear_config_cache()
    cfg = settings.load_config(allow_stub_telegram=True)

    summary = tokens.build_auth_posture_summary(cfg, cors_origins_count=3)

    assert summary["allowed_user_ids_configured"] is True
    assert summary["allowed_user_ids_count"] == 2
    assert summary["allowed_client_ids_configured"] is True
    assert summary["allowed_client_ids_count"] == 2
    assert summary["cors_origins_count"] == 3
    assert "111" not in repr(summary)
    assert "web-v1" not in repr(summary)
