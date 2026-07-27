from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "ops/docker/docker-compose.yml").read_text(encoding="utf-8"))


def test_provider_browsers_have_separate_internal_displays_and_no_published_control_ports() -> None:
    compose = _compose()
    services = compose["services"]

    for provider in ("chatgpt", "claude"):
        display = services[f"ai-backup-display-{provider}"]
        browser = services[f"cloakbrowser-reauth-{provider}"]
        control_network = f"ai_backup_control_{provider}"
        egress_network = f"ai_backup_browser_egress_{provider}"
        assert display["build"]["dockerfile"] == "ops/docker/ai-backup-display/Dockerfile"
        assert display["networks"] == [control_network]
        assert "ports" not in display
        assert display["healthcheck"]["test"][-1].endswith("healthcheck.sh")
        assert (
            "@sha256:a333b754fe9da1fd16851f2bb69f258601d4c7fa36e8b26c15f1e031241076c1"
            in browser["image"]
        )
        assert browser["environment"] == ["CLOAKBROWSER_AUTO_UPDATE=false", "DISPLAY=:99"]
        assert set(browser["networks"]) == {control_network, egress_network}
        assert browser["command"] == ["--port=9222", "--headless=false"]
        assert browser["entrypoint"] == [
            "sh",
            "/usr/local/bin/ratatoskr-cloakserve-headed",
        ]
        assert any(
            "cloakbrowser-reauth/entrypoint.sh" in volume
            and volume.endswith(":ro")
            for volume in browser["volumes"]
        )
        assert "ports" not in browser
        assert "/json/version" not in " ".join(browser["healthcheck"]["test"])
        assert "http://localhost:9222/" in " ".join(browser["healthcheck"]["test"])

    assert compose["networks"]["ai_backup_control_chatgpt"]["internal"] is True
    assert compose["networks"]["ai_backup_control_claude"]["internal"] is True
    assert compose["services"]["mobile-api"]["networks"] == [
        "default",
        "ai_backup_control_chatgpt",
        "ai_backup_control_claude",
    ]
    assert (
        "CSP_CONNECT_SRC_EXTRA=${CSP_CONNECT_SRC_EXTRA:-wss://ratatoskr.po4yka.com}"
        in compose["services"]["mobile-api"]["environment"]
    )
    assert "ai-backup-display-chatgpt" in compose["volumes"]
    assert "ai-backup-display-claude" in compose["volumes"]


def test_display_image_is_digest_pinned_and_starts_xvnc_with_openbox() -> None:
    dockerfile = (ROOT / "ops/docker/ai-backup-display/Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "ops/docker/ai-backup-display/entrypoint.sh").read_text(encoding="utf-8")
    openbox = (ROOT / "ops/docker/ai-backup-display/openbox-rc.xml").read_text(encoding="utf-8")

    assert "debian:trixie-slim@sha256:" in dockerfile
    assert "tigervnc-standalone-server" in dockerfile
    assert "openbox" in dockerfile
    assert "Xtigervnc :99" in entrypoint
    assert "-geometry 1920x1080" in entrypoint
    assert "-SecurityTypes None" in entrypoint
    assert "openbox --config-file" in entrypoint
    assert "/proc/net/tcp" in (
        ROOT / "ops/docker/ai-backup-display/healthcheck.sh"
    ).read_text(encoding="utf-8")
    assert "<maximized>yes</maximized>" in openbox


def test_headed_cloakbrowser_wrapper_fixes_only_the_pinned_false_flag_bug() -> None:
    wrapper = (ROOT / "ops/docker/cloakbrowser-reauth/entrypoint.sh").read_text(
        encoding="utf-8"
    )

    assert "text.count(bug) != 1" in wrapper
    assert 'config["headless"] = False' in wrapper
    assert "unsupported CloakBrowser cloakserve parser" in wrapper


def test_pi_deploy_orders_display_then_browser_then_mobile_api_and_keeps_them_isolated() -> None:
    script = (ROOT / "tools/scripts/build-and-deploy-pi.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "DISPLAY_DOCKERFILE=ops/docker/ai-backup-display/Dockerfile" in script
    assert "DISPLAY_SERVICES=(ai-backup-display-chatgpt ai-backup-display-claude)" in script
    assert "BROWSER_SERVICES=(cloakbrowser-reauth-chatgpt cloakbrowser-reauth-claude)" in script
    assert 'build_and_ship "$DISPLAY_DOCKERFILE" -- "${DISPLAY_TO_BUILD[@]}"' in script
    assert "verify_pinned_cloakbrowser_image" in script
    assert "verify_remote_checkout" in script
    assert "verify_headed_browser_runtime" in script
    assert (
        "MOBILE_API_CONTROL_NETWORKS=(ai_backup_control_chatgpt "
        "ai_backup_control_claude)" in script
    )
    assert "ensure_mobile_api_control_networks" in script
    assert "docker network connect --alias 'mobile-api'" in script
    assert script.index('restart_service_verified "$svc"') < script.index(
        'ensure_mobile_api_control_networks "$svc"'
    )
    assert script.index('ensure_mobile_api_control_networks "$svc"') < script.index(
        'wait_for_service_health "$svc"'
    )
    assert "/json/version?fingerprint=" in script
    assert "deadbeef0001" in script
    assert "deadbeef0002" in script
    assert "f18e241fb1fb" not in script
    assert "4476d4318027" not in script
    assert "pkill -TERM -f '[/]chrome'" in script
    assert "--ozone-platform=x11" in script
    assert "git diff --quiet -- ops/docker/cloakbrowser-reauth/entrypoint.sh ops/docker/docker-compose.yml" in script
    assert "is_isolated_reauth_service" in script
    assert (
        '--services "ai-backup-display-chatgpt ai-backup-display-claude cloakbrowser-reauth-chatgpt cloakbrowser-reauth-claude ratatoskr worker scheduler mobile-api pg-backup"'
        in makefile
    )


def test_pi_deploy_rejects_pinned_browser_rollback_before_remote_mutation() -> None:
    script = (ROOT / "tools/scripts/build-and-deploy-pi.sh").read_text(encoding="utf-8")
    guard = "rollback is not supported for digest-pinned CloakBrowser services"

    assert script.index(guard) < script.index('echo "==> Verifying SSH')
