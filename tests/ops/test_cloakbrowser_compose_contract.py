from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_cloakbrowser_clears_stale_xvfb_state_before_startup() -> None:
    compose = yaml.safe_load((ROOT / "ops/docker/docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["cloakbrowser"]
    wrapper = (ROOT / "ops/docker/cloakbrowser/restart-safe-entrypoint.sh").read_text(
        encoding="utf-8"
    )

    assert service["entrypoint"] == [
        "/bin/bash",
        "/usr/local/bin/ratatoskr-cloakbrowser-entrypoint.sh",
    ]
    assert (
        "./cloakbrowser/restart-safe-entrypoint.sh:"
        "/usr/local/bin/ratatoskr-cloakbrowser-entrypoint.sh:ro"
    ) in service["volumes"]
    assert "rm -f -- /tmp/.X99-lock /tmp/.X11-unix/X99" in wrapper
    assert 'exec /entrypoint.sh "$@"' in wrapper
