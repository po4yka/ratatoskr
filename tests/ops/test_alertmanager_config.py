from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/monitoring/render-alertmanager-config.sh"
TEMPLATE = ROOT / "ops/monitoring/alertmanager.yml"
RECEIVER_ENV = (
    "ALERT_WEBHOOK_URL",
    "ALERT_SLACK_API_URL",
    "ALERT_TELEGRAM_WEBHOOK_URL",
    "ALERT_PAGERDUTY_ROUTING_KEY",
)


def _run_renderer(
    tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    rendered = tmp_path / "alertmanager.yml"
    env = os.environ.copy()
    for name in RECEIVER_ENV:
        env.pop(name, None)
    env.update(
        {
            "ALERTMANAGER_CONFIG_TEMPLATE": str(TEMPLATE),
            "ALERTMANAGER_CONFIG_RENDERED": str(rendered),
            "ALERTMANAGER_BIN": shutil.which("true") or "/bin/true",
            **overrides,
        }
    )
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    return result, rendered


def test_production_without_receiver_fails_closed(tmp_path: Path) -> None:
    result, rendered = _run_renderer(tmp_path, RATATOSKR_ENV="production")

    assert result.returncode != 0
    assert "no production receiver configured" in result.stderr
    assert not rendered.exists()


def test_production_renders_every_supported_receiver(tmp_path: Path) -> None:
    secrets = {
        "ALERT_WEBHOOK_URL": "http://alerts.internal.example/v1/notify?token=webhook-secret",
        "ALERT_SLACK_API_URL": "https://hooks.slack.example/services/slack-secret",
        "ALERT_TELEGRAM_WEBHOOK_URL": "https://telegram.example/notify/telegram-secret",
        "ALERT_PAGERDUTY_ROUTING_KEY": "pagerduty_secret_123",
    }
    result, rendered = _run_renderer(
        tmp_path,
        RATATOSKR_ENV="production",
        **secrets,
    )

    assert result.returncode == 0, result.stderr
    config = yaml.safe_load(rendered.read_text(encoding="utf-8"))
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    receiver = config["receivers"][0]
    assert config["route"]["receiver"] == "configured"
    assert [item["url"] for item in receiver["webhook_configs"]] == [
        secrets["ALERT_WEBHOOK_URL"],
        secrets["ALERT_TELEGRAM_WEBHOOK_URL"],
    ]
    assert receiver["slack_configs"][0]["api_url"] == secrets["ALERT_SLACK_API_URL"]
    assert receiver["pagerduty_configs"][0]["routing_key"] == secrets["ALERT_PAGERDUTY_ROUTING_KEY"]
    assert all(secret not in result.stdout + result.stderr for secret in secrets.values())


def test_invalid_receiver_fails_without_logging_secret(tmp_path: Path) -> None:
    invalid_secret = "http://hooks.slack.example/services/do-not-log"
    result, _ = _run_renderer(
        tmp_path,
        RATATOSKR_ENV="production",
        ALERT_SLACK_API_URL=invalid_secret,
    )

    assert result.returncode != 0
    assert "ALERT_SLACK_API_URL is invalid" in result.stderr
    assert invalid_secret not in result.stdout + result.stderr


def test_development_uses_explicit_discard_receiver(tmp_path: Path) -> None:
    result, rendered = _run_renderer(tmp_path, RATATOSKR_ENV="development")

    assert result.returncode == 0, result.stderr
    config = yaml.safe_load(rendered.read_text(encoding="utf-8"))
    assert config["receivers"][0]["webhook_configs"][0]["url"] == (
        "http://127.0.0.1:9/alertmanager-unconfigured"
    )
