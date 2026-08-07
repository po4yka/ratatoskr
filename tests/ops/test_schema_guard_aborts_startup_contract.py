"""The Alembic schema-head guard must actually stop the service from starting.

Every long-running service wraps its entrypoint in `sh -c` behind
`python -m app.cli.migrate_db --check`. That check is the only thing standing
between a stale schema and a running process: `tools/scripts/build-and-deploy-pi.sh`
no longer applies migrations, and `ops/docker/Dockerfile.api` documents migration
application as an explicit operator step rather than a startup side effect.

`sh -c` runs each line of a multi-line script regardless of the previous line's
exit code. Without `set -e` the guard logged the drift, returned 1, and the
`exec` on the next line replaced the shell anyway -- bot, worker, and mobile-api
all booted against the unmigrated schema and the container still reported exit 0.

So this file does not grep for `set -e`. It extracts each service's real script,
stubs only the two payload commands, and runs it under a real `sh` -- the same
thing the container does. Any prologue that aborts on failure passes; any that
does not fails, whichever shell idiom it uses.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

_CHECK_COMMAND = "python -m app.cli.migrate_db --check"
_RAN_MARKER = "GUARDED_COMMAND_RAN"


class _ComposeLoader(yaml.SafeLoader):
    """Load Compose merge tags as their underlying YAML values."""


def _construct_override(loader: _ComposeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    raise TypeError(f"Unsupported Compose override node: {type(node).__name__}")


_ComposeLoader.add_constructor("!override", _construct_override)


def _compose(filename: str) -> dict:
    return yaml.load(
        (ROOT / "ops/docker" / filename).read_text(encoding="utf-8"), Loader=_ComposeLoader
    )


def _guarded_scripts() -> dict[str, str]:
    """Compose service -> the `sh -c` script that runs the schema guard."""
    scripts: dict[str, str] = {}
    for name, svc in _compose("docker-compose.yml")["services"].items():
        command = svc.get("command")
        if not isinstance(command, list) or command[:2] != ["sh", "-c"]:
            continue
        script = command[2]
        if _CHECK_COMMAND in script:
            scripts[name] = script
    return scripts


_GUARDED = _guarded_scripts()


def _stub(script: str, *, check_passes: bool) -> str:
    """Replace the two payload commands so the script can run outside a container.

    Everything else -- notably the prologue that decides whether a failed check
    aborts -- is the shipped text, unmodified.
    """
    stubbed = script.replace(_CHECK_COMMAND, "true" if check_passes else "false")
    return re.sub(r"^exec .*$", f"echo {_RAN_MARKER}", stubbed, flags=re.MULTILINE)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    # Fixed argv; the script is the repo's own compose text with stubbed payloads.
    return subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_derivation_found_the_services() -> None:
    """A derivation that matches nothing would make every assertion below vacuous."""
    assert len(_GUARDED) >= 3, f"only found {sorted(_GUARDED)} -- the guard has moved or gone"
    for expected in ("ratatoskr", "worker", "mobile-api"):
        assert expected in _GUARDED, f"{expected} lost its schema-head guard"


@pytest.mark.parametrize("service", sorted(_GUARDED))
def test_a_failed_check_stops_the_service(service: str) -> None:
    result = _run(_stub(_GUARDED[service], check_passes=False))

    assert _RAN_MARKER not in result.stdout, (
        f"{service} starts its entrypoint even though the schema check failed -- "
        "the guard is decorative; the script needs to abort on a non-zero check"
    )
    assert result.returncode != 0, (
        f"{service} reports success after a failed schema check, so Docker sees a "
        f"clean exit instead of a failing container (exit {result.returncode})"
    )


@pytest.mark.parametrize("service", sorted(_GUARDED))
def test_a_passing_check_still_starts_the_service(service: str) -> None:
    """Positive control: a guard that blocks everything would pass the test above."""
    result = _run(_stub(_GUARDED[service], check_passes=True))

    assert _RAN_MARKER in result.stdout, (
        f"{service} does not reach its entrypoint even when the schema check passes: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
    assert result.returncode == 0
