from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from app.config import Settings, clear_config_cache, load_config
from app.config.config_file import load_ratatoskr_yaml

pytestmark = pytest.mark.uses_real_yaml


def _active_env_assignments(path: Path) -> list[str]:
    assignments: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        assignments.append(stripped.split("=", 1)[0])
    return assignments


def test_env_example_exposes_only_first_run_required_assignments() -> None:
    env_vars = _active_env_assignments(Path(".env.example"))

    assert len(env_vars) <= 9
    assert env_vars == [
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "ALLOWED_USER_IDS",
        "POSTGRES_PASSWORD",
        "DATABASE_URL",
        "OPENROUTER_API_KEY",
    ]


def test_ratatoskr_yaml_loads_nested_power_user_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "ratatoskr.yaml"
    cfg.write_text(
        dedent(
            """\
            runtime:
              log_level: DEBUG
              request_timeout_sec: 45
            scraper:
              profile: robust
              provider_order:
                - scrapling
                - direct_html
            youtube:
              enabled: false
              subtitle_languages:
                - en
                - ru
            twitter:
              enabled: false
            mcp:
              enabled: true
              transport: sse
              user_id: 12345
            openrouter:
              model: qwen/qwen3-max
            ollama:
              base_url: https://ollama.example.com/v1
              api_key: cloud-secret
              model: llama3.3
              enable_structured_outputs: false
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RATATOSKR_CONFIG", str(cfg))

    result = load_ratatoskr_yaml(Settings)

    assert result["LOG_LEVEL"] == "DEBUG"
    assert result["REQUEST_TIMEOUT_SEC"] == "45"
    assert result["SCRAPER_PROFILE"] == "robust"
    assert result["SCRAPER_PROVIDER_ORDER"] == "scrapling,direct_html"
    assert result["YOUTUBE_DOWNLOAD_ENABLED"] == "false"
    assert result["YOUTUBE_SUBTITLE_LANGUAGES"] == "en,ru"
    assert result["TWITTER_ENABLED"] == "false"
    assert result["MCP_ENABLED"] == "true"
    assert result["MCP_USER_ID"] == "12345"
    assert result["OPENROUTER_MODEL"] == "qwen/qwen3-max"
    assert result["OLLAMA_BASE_URL"] == "https://ollama.example.com/v1"
    assert result["OLLAMA_API_KEY"] == "cloud-secret"
    assert result["OLLAMA_MODEL"] == "llama3.3"
    assert result["OLLAMA_ENABLE_STRUCTURED_OUTPUTS"] == "false"


def test_ratatoskr_yaml_overrides_process_environment_for_non_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-refactor: YAML wins over env for non-secret fields.

    The reverse case (secrets stay in env, YAML secret keys are ignored) is
    covered by tests/config/test_yaml_secret_precedence.py. Pre-refactor this
    test asserted env > YAML; the new precedence is non-secret YAML > env.
    """
    cfg = tmp_path / "ratatoskr.yaml"
    cfg.write_text(
        dedent(
            """\
            runtime:
              log_level: DEBUG
            openrouter:
              model: qwen/qwen3-max
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RATATOSKR_CONFIG", str(cfg))
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")

    env = {
        "API_ID": "123456",
        "API_HASH": "a" * 32,
        "BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz0123456789abcdefghij",
        "OPENROUTER_API_KEY": "or_" + "b" * 20,
        "ALLOWED_USER_IDS": "123",
        "LOG_LEVEL": "WARNING",
    }
    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            mp.setenv(key, value)
        mp.setenv("RATATOSKR_CONFIG", str(cfg))
        mp.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

    app_config = settings.as_app_config()
    # Both LOG_LEVEL and OPENROUTER_MODEL are non-secret -> YAML wins.
    assert app_config.runtime.log_level == "DEBUG"
    assert app_config.openrouter.model == "qwen/qwen3-max"


def test_missing_required_startup_config_lists_exact_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RATATOSKR_CONFIG", raising=False)
    with pytest.MonkeyPatch.context() as mp:
        for key in tuple(os.environ):
            mp.delenv(key, raising=False)
        clear_config_cache()
        with pytest.raises(RuntimeError) as exc_info:
            load_config()

    message = str(exc_info.value)
    for name in (
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "ALLOWED_USER_IDS",
        "OPENROUTER_API_KEY",
    ):
        assert name in message
    assert ".env.example" in message
    assert "docs/reference/config-file.md" in message


def test_deprecated_phase1_env_vars_raise_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIGRATION_SHADOW_MODE_ENABLED", "true")

    with pytest.raises(RuntimeError, match="MIGRATION_SHADOW_MODE_ENABLED"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_deprecated_phase1_env_vars_in_dotenv_raise_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "MIGRATION_SHADOW_MODE_ENABLED=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIGRATION_SHADOW_MODE_ENABLED", raising=False)
    clear_config_cache()

    with pytest.raises(RuntimeError, match="MIGRATION_SHADOW_MODE_ENABLED"):
        load_config()
