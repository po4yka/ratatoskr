"""Verify every running service is attached to the networks Compose says it needs.

Docker attaches the first network when it creates a container and the rest after
it starts. When one of those later attachments does not happen, the container
comes up on a subset of its networks -- and Compose does not notice, because the
container exists and is running.

Observed on this deployment: a recreate left ``mobile-api`` and the bot off
``docker_default``, so ``postgres`` stopped resolving and both crash-looped; a
second recreate fixed those two and dropped the same network from ``worker``
instead. The quiet case is worse -- ``mobile-api`` also came up without
``ai_backup_control_chatgpt``, stayed healthy, and would have failed only later,
whenever a ChatGPT AI-backup ran, looking like an unrelated fault.

Desired state comes from ``docker compose config``, which resolves each network's
real name (project prefix, explicit ``name:``, or ``external``) so this does not
re-implement Compose's naming rules. Only running services are checked: the
question is whether what is deployed matches what was asked for.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Every profile in the compose file. `docker compose config` omits profile-gated
# services unless their profile is on, and a service that is *running* must be
# checked whether or not the caller remembered its profile.
_ALL_PROFILES = (
    "ai-backup-reauth",
    "mcp",
    "mcp-public",
    "mcp-write",
    "with-cloud-ollama",
    "with-monitoring",
    "with-scrapers",
    "with-webwright",
)


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(f"command failed: {' '.join(args)}\n{result.stderr}\n")
        raise SystemExit(2)
    return result.stdout


def _compose_base(files: list[str]) -> list[str]:
    argv = ["docker", "compose"]
    for path in files:
        argv += ["-f", path]
    for profile in _ALL_PROFILES:
        argv += ["--profile", profile]
    return argv


def _desired(files: list[str]) -> dict[str, set[str]]:
    """Service -> the real Docker network names it should be attached to."""
    config = json.loads(_run([*_compose_base(files), "config", "--format", "json"]))
    resolved = {
        key: (value or {}).get("name") or key
        for key, value in (config.get("networks") or {}).items()
    }
    desired: dict[str, set[str]] = {}
    for name, service in (config.get("services") or {}).items():
        if service.get("network_mode"):
            # `network_mode: none` (or host/container:) opts out of the network
            # model entirely -- the ai-backup webauthn bridges run isolated on
            # purpose. Expecting the default network here reported them as broken.
            desired[name] = set()
            continue
        keys = service.get("networks")
        # A service with no `networks:` key joins the default network.
        wanted = list(keys) if keys else ["default"]
        desired[name] = {resolved.get(key, key) for key in wanted}
    return desired


def _actual(files: list[str]) -> dict[str, set[str]]:
    """Running service -> the networks its container is actually on."""
    ids = _run([*_compose_base(files), "ps", "-q"]).split()
    if not ids:
        return {}
    raw = _run(["docker", "inspect", *ids])
    actual: dict[str, set[str]] = {}
    for container in json.loads(raw):
        if not (container.get("State") or {}).get("Running"):
            continue
        labels = (container.get("Config") or {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if not service:
            continue
        networks = ((container.get("NetworkSettings") or {}).get("Networks") or {}).keys()
        actual[service] = set(networks)
    return actual


def _container_id(files: list[str], service: str) -> str | None:
    ids = _run([*_compose_base(files), "ps", "-q", service]).split()
    return ids[0] if ids else None


def _attach(files: list[str], service: str, network: str) -> bool:
    """Connect one missing network, keeping the Compose service alias.

    The alias is load-bearing: Prometheus and the status probes address services
    by Compose service name, so an attachment without it restores routing but
    not discovery.
    """
    container = _container_id(files, service)
    if container is None:
        return False
    result = subprocess.run(
        ["docker", "network", "connect", "--alias", service, network, container],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print(f"    attached {service} -> {network}")
        return True
    sys.stderr.write(f"    could not attach {service} -> {network}: {result.stderr.strip()}\n")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        dest="files",
        required=True,
        help="compose file (repeatable, in the same order as the deploy)",
    )
    parser.add_argument(
        "--service",
        help="check only this service (the deploy repairs one service at a time)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="attach missing networks instead of only reporting them",
    )
    args = parser.parse_args()

    desired = _desired(args.files)
    actual = _actual(args.files)
    if args.service:
        actual = {k: v for k, v in actual.items() if k == args.service}
    if not actual:
        print("    no running services found -- nothing to check")
        return 0

    problems: list[str] = []
    for service in sorted(actual):
        want = desired.get(service)
        if want is None:
            # Running but absent from the config: a leftover from an older
            # revision. Report it rather than silently passing.
            problems.append(f"{service}: running but not defined in the compose files")
            continue
        missing = want - actual[service]
        if missing and args.fix:
            # Re-attach rather than report: this runs inside the deploy, right
            # after the recreate that dropped them and before the health wait.
            missing = {net for net in missing if not _attach(args.files, service, net)}
        if missing:
            problems.append(
                f"{service}: missing {', '.join(sorted(missing))} "
                f"(attached: {', '.join(sorted(actual[service])) or 'none'})"
            )

    if not problems:
        label = args.service or f"{len(actual)} running services"
        print(f"    networks OK for {label}")
        return 0

    sys.stderr.write("ERROR: container network attachments do not match compose\n")
    for problem in problems:
        sys.stderr.write(f"  - {problem}\n")
    sys.stderr.write(
        "\n  A container on a subset of its networks still runs and can still report\n"
        "  healthy; it fails later, on whatever it cannot resolve. Recreate the\n"
        "  affected service WITHOUT --no-deps (that flag skips network\n"
        "  reconciliation), and if it is crash-looping remove it first -- a\n"
        "  restarting container has no network sandbox to attach to:\n"
        "    docker rm -f <container> && docker compose <-f ...> up -d <service>\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
