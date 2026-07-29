from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest
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
        assert browser["environment"] == [
            "CLOAKBROWSER_AUTO_UPDATE=false",
            "DISPLAY=:99",
            "DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/ratatoskr-dbus/system_bus_socket",
        ]
        assert set(browser["networks"]) == {control_network, egress_network}
        assert browser["command"] == ["--port=9222", "--headless=false"]
        assert browser["entrypoint"] == [
            "sh",
            "/usr/local/bin/ratatoskr-cloakserve-headed",
        ]
        assert any(
            "cloakbrowser-reauth/entrypoint.sh" in volume and volume.endswith(":ro")
            for volume in browser["volumes"]
        )
        assert f"ai-backup-webauthn-dbus-{provider}:/run/ratatoskr-dbus:ro" in browser["volumes"]
        assert browser["depends_on"][f"ai-backup-webauthn-bridge-{provider}"] == {
            "condition": "service_healthy"
        }
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
    assert "ai-backup-webauthn-dbus-chatgpt" in compose["volumes"]
    assert "ai-backup-webauthn-dbus-claude" in compose["volumes"]


def test_provider_webauthn_bridges_expose_only_filtered_bluez_without_network() -> None:
    compose = _compose()
    for provider in ("chatgpt", "claude"):
        bridge = compose["services"][f"ai-backup-webauthn-bridge-{provider}"]
        assert bridge["build"]["dockerfile"] == ("ops/docker/ai-backup-webauthn-bridge/Dockerfile")
        assert bridge["network_mode"] == "none"
        assert "ports" not in bridge
        assert bridge["read_only"] is True
        assert bridge["cap_drop"] == ["ALL"]
        assert bridge["security_opt"] == ["no-new-privileges:true"]
        assert (
            "/run/dbus/system_bus_socket:/run/host-dbus/system_bus_socket:ro" in bridge["volumes"]
        )
        assert f"ai-backup-webauthn-dbus-{provider}:/run/ratatoskr-dbus" in bridge["volumes"]
        assert bridge["healthcheck"]["test"][-1].endswith("healthcheck.sh")

    dockerfile = (ROOT / "ops/docker/ai-backup-webauthn-bridge/Dockerfile").read_text(
        encoding="utf-8"
    )
    entrypoint = (ROOT / "ops/docker/ai-backup-webauthn-bridge/entrypoint.sh").read_text(
        encoding="utf-8"
    )
    healthcheck = (ROOT / "ops/docker/ai-backup-webauthn-bridge/healthcheck.sh").read_text(
        encoding="utf-8"
    )

    assert "debian:trixie-slim@sha256:" in dockerfile
    assert "xdg-dbus-proxy" in dockerfile
    assert "dbus-bin" in dockerfile
    assert "--filter" in entrypoint
    assert "--talk=org.bluez" in entrypoint
    assert "/run/host-dbus/system_bus_socket" in entrypoint
    assert "AI_BACKUP_WEBAUTHN_DBUS_SOCKET" in healthcheck
    assert '--bus="unix:path=${bus_socket}"' in healthcheck
    assert "--address=" not in healthcheck
    assert "--dest=org.bluez" in healthcheck


@pytest.mark.parametrize(
    ("hci0_powered", "hci1_powered", "expected_returncode"),
    [("true", "false", 0), ("false", "false", 1), ("false", "true", 0)],
)
def test_webauthn_bridge_health_accepts_any_powered_adapter(
    tmp_path: Path,
    hci0_powered: str,
    hci1_powered: str,
    expected_returncode: int,
) -> None:
    bus_socket = (
        Path("/tmp")
        / f"ratatoskr-webauthn-{os.getpid()}-{hci0_powered}-{hci1_powered}.sock"
    )
    bus_socket.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(bus_socket))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_dbus_send = fake_bin / "dbus-send"
    fake_dbus_send.write_text(
        """#!/bin/sh
case "$*" in
  *org.freedesktop.DBus.ObjectManager.GetManagedObjects*)
    printf '%s\n' \
      'method return time=0.0 sender=:1.1 -> destination=:1.2 serial=1 reply_serial=1' \
      '   array [' \
      '      dict entry(' \
      '         object path "/org/bluez/hci0"' \
      '         array [' \
      '            dict entry(' \
      '               string "org.bluez.Adapter1"' \
      '      dict entry(' \
      '         object path "/org/bluez/hci1"' \
      '         array [' \
      '            dict entry(' \
      '               string "org.bluez.Adapter1"'
    ;;
  *org.freedesktop.DBus.Properties.Get*)
    case "$*" in
      */org/bluez/hci0*) powered=${FAKE_HCI0_POWERED} ;;
      */org/bluez/hci1*) powered=${FAKE_HCI1_POWERED} ;;
      *) exit 3 ;;
    esac
    printf '%s\n' \
      'method return time=0.0 sender=:1.1 -> destination=:1.2 serial=2 reply_serial=2' \
      "   variant       boolean ${powered}"
    ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    fake_dbus_send.chmod(0o755)

    env = {
        **os.environ,
        "AI_BACKUP_WEBAUTHN_DBUS_SOCKET": str(bus_socket),
        "FAKE_HCI0_POWERED": hci0_powered,
        "FAKE_HCI1_POWERED": hci1_powered,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    try:
        result = subprocess.run(
            ["sh", str(ROOT / "ops/docker/ai-backup-webauthn-bridge/healthcheck.sh")],
            check=False,
            env=env,
        )
    finally:
        listener.close()
        bus_socket.unlink(missing_ok=True)

    assert result.returncode == expected_returncode


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
    assert "/proc/net/tcp" in (ROOT / "ops/docker/ai-backup-display/healthcheck.sh").read_text(
        encoding="utf-8"
    )
    assert "<maximized>yes</maximized>" in openbox


def test_headed_cloakbrowser_wrapper_fixes_only_the_pinned_false_flag_bug() -> None:
    wrapper = (ROOT / "ops/docker/cloakbrowser-reauth/entrypoint.sh").read_text(encoding="utf-8")

    assert "text.count(bug) != 1" in wrapper
    assert 'config["headless"] = False' in wrapper
    assert "unsupported CloakBrowser cloakserve parser" in wrapper


def test_pi_deploy_orders_display_then_browser_then_mobile_api_and_keeps_them_isolated() -> None:
    script = (ROOT / "tools/scripts/build-and-deploy-pi.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "DISPLAY_DOCKERFILE=ops/docker/ai-backup-display/Dockerfile" in script
    assert "DISPLAY_SERVICES=(ai-backup-display-chatgpt ai-backup-display-claude)" in script
    assert "WEBAUTHN_DOCKERFILE=ops/docker/ai-backup-webauthn-bridge/Dockerfile" in script
    assert (
        "WEBAUTHN_SERVICES=(ai-backup-webauthn-bridge-chatgpt "
        "ai-backup-webauthn-bridge-claude)" in script
    )
    assert "BROWSER_SERVICES=(cloakbrowser-reauth-chatgpt cloakbrowser-reauth-claude)" in script
    assert 'build_and_ship "$DISPLAY_DOCKERFILE" -- "${DISPLAY_TO_BUILD[@]}"' in script
    assert 'build_and_ship "$WEBAUTHN_DOCKERFILE" -- "${WEBAUTHN_TO_BUILD[@]}"' in script
    assert "verify_pinned_cloakbrowser_image" in script
    assert "verify_remote_checkout" in script
    assert "verify_headed_browser_runtime" in script
    assert "verify_webauthn_host" in script
    assert "verify_webauthn_bridge_runtime" in script
    assert "add_reauth_prerequisites" in script
    assert 'service_requested "$display" || prerequisites+=("$display")' in script
    assert 'service_requested "$bridge" || prerequisites+=("$bridge")' in script
    assert (
        "MOBILE_API_CONTROL_NETWORKS=(ai_backup_control_chatgpt ai_backup_control_claude)" in script
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
    assert "ops/docker/ai-backup-webauthn-bridge/Dockerfile" in script
    assert "is_isolated_reauth_service" in script
    assert (
        '--services "ai-backup-display-chatgpt ai-backup-display-claude ai-backup-webauthn-bridge-chatgpt ai-backup-webauthn-bridge-claude cloakbrowser-reauth-chatgpt cloakbrowser-reauth-claude ratatoskr worker scheduler mobile-api pg-backup"'
        in makefile
    )


def test_pi_deploy_rejects_pinned_browser_rollback_before_remote_mutation() -> None:
    script = (ROOT / "tools/scripts/build-and-deploy-pi.sh").read_text(encoding="utf-8")
    guard = "rollback is not supported for digest-pinned CloakBrowser services"

    assert script.index(guard) < script.index('echo "==> Verifying SSH')


def test_pi_deploy_resolves_each_browser_after_its_provider_prerequisites() -> None:
    deploy = ROOT / "tools/scripts/build-and-deploy-pi.sh"

    for provider in ("chatgpt", "claude"):
        display = f"ai-backup-display-{provider}"
        bridge = f"ai-backup-webauthn-bridge-{provider}"
        browser = f"cloakbrowser-reauth-{provider}"
        for requested in (browser, f"{browser} {bridge} {display}"):
            completed = subprocess.run(
                [str(deploy), "--services", requested, "--resolve-services"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            assert completed.stdout.splitlines() == [display, bridge, browser]


def test_pi_deploy_rejects_unknown_service_while_resolving_order() -> None:
    deploy = ROOT / "tools/scripts/build-and-deploy-pi.sh"

    completed = subprocess.run(
        [str(deploy), "--service", "definitely-not-a-service", "--resolve-services"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "unsupported service: definitely-not-a-service" in completed.stderr
