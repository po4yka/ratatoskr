"""Tracing that is on by default needs a collector that actually resolves.

``OTEL_EXPORTER_OTLP_ENDPOINT`` has always defaulted to ``http://tempo:4317``,
but tempo lived only in ``docker-compose.monitoring.yml``, on that file's own
``monitoring`` network. The production path is the ``with-monitoring`` profile in
the primary Compose file (ops/monitoring/README.md), which never created it. So
five services pushed spans at a name that did not resolve: buffered, dropped at
every restart, and paid for on every shutdown.

That stayed invisible while ``OTEL_ENABLED`` defaulted to false. It no longer
does, so the coupling is now load-bearing and checked here rather than trusted:
whatever host the endpoint names has to be a service in the same file, reachable
from the services that push to it.

Nothing below hardcodes "tempo". The host is read out of the endpoint the
services actually ship, so renaming the collector or repointing the endpoint
keeps the check honest instead of quietly making it vacuous.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

_ENABLED = "OTEL_ENABLED"
_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"


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


def _compose() -> dict:
    return yaml.load(
        (ROOT / "ops/docker/docker-compose.yml").read_text(encoding="utf-8"),
        Loader=_ComposeLoader,
    )


def _environment(service: dict) -> dict[str, str]:
    return dict(item.split("=", 1) for item in service.get("environment", []) if "=" in item)


def _default_of(value: str) -> str:
    """`${VAR:-fallback}` -> `fallback`; anything else is already literal."""
    if value.startswith("${") and ":-" in value and value.endswith("}"):
        return value.split(":-", 1)[1][:-1]
    return value


def _networks(service: dict) -> set[str]:
    declared = service.get("networks")
    if not declared:
        return {"default"}
    names = declared if isinstance(declared, list) else list(declared)
    return set(names) | {"default"} if "default" in names else set(names)


_SERVICES = _compose()["services"]
_TRACING_CLIENTS = {name: svc for name, svc in _SERVICES.items() if _ENDPOINT in _environment(svc)}
_COLLECTOR_HOSTS = {
    urlparse(_default_of(_environment(svc)[_ENDPOINT])).hostname
    for svc in _TRACING_CLIENTS.values()
}


def test_the_derivation_found_the_tracing_services() -> None:
    """A derivation that matches nothing would make every assertion below vacuous."""
    assert len(_TRACING_CLIENTS) >= 5, f"only found {sorted(_TRACING_CLIENTS)}"
    assert _COLLECTOR_HOSTS and None not in _COLLECTOR_HOSTS, (
        f"could not read a collector host out of {_ENDPOINT}"
    )


def test_every_service_agrees_on_one_collector() -> None:
    """Split endpoints would mean half the traces land somewhere nobody looks."""
    assert len(_COLLECTOR_HOSTS) == 1, f"services disagree on the collector: {_COLLECTOR_HOSTS}"


@pytest.mark.parametrize("host", sorted(h for h in _COLLECTOR_HOSTS if h))
def test_the_collector_is_defined_in_the_same_file(host: str) -> None:
    assert host in _SERVICES, (
        f"{len(_TRACING_CLIENTS)} services export spans to '{host}', which this file "
        f"never creates. That is the bug this contract exists for: the name resolved "
        f"nowhere and every span was dropped."
    )


@pytest.mark.parametrize("host", sorted(h for h in _COLLECTOR_HOSTS if h))
def test_the_collector_shares_a_network_with_its_clients(host: str) -> None:
    """Being in the file is not enough; it has to be on the clients' network."""
    collector = _networks(_SERVICES[host])
    for name, svc in _TRACING_CLIENTS.items():
        assert collector & _networks(svc), (
            f"{name} cannot reach '{host}': {sorted(_networks(svc))} vs {sorted(collector)}"
        )


_DEPLOY = ROOT / "tools/scripts/build-and-deploy-pi.sh"


@pytest.mark.parametrize("host", sorted(h for h in _COLLECTOR_HOSTS if h))
def test_the_deploy_starts_the_collector(host: str) -> None:
    """The other half of the coupling, and the half a Compose file cannot hold.

    Everything above checks that the repository agrees with itself. None of it
    says anything about how the stack is actually launched, and the deploy brings
    services up by name -- so a collector that exists in the file but is never
    named simply never runs.
    """
    script = _DEPLOY.read_text(encoding="utf-8")

    assert re.search(rf"up -d[^\n]*\b{re.escape(host)}\b", script), (
        f"the deploy never starts '{host}', so the services it ships come up "
        f"exporting spans to nothing"
    )


@pytest.mark.parametrize("host", sorted(h for h in _COLLECTOR_HOSTS if h))
def test_the_deploy_passes_every_profile_the_collector_needs(host: str) -> None:
    """`up tempo` fails with "no such service" if its profile is not enabled."""
    script = _DEPLOY.read_text(encoding="utf-8")

    for profile in _SERVICES[host].get("profiles") or []:
        assert f"--profile {profile}" in script, (
            f"'{host}' is gated behind the '{profile}' profile, which the deploy "
            f"does not enable -- starting it would fail with 'no such service'"
        )


def test_tracing_on_by_default_keeps_the_collector_in_the_same_profile() -> None:
    """If the spans start flowing unasked, the sink must come up with them.

    Both halves matter. Tracing on with no collector in the profile is the
    original defect; a collector in a profile the clients do not share would be
    the same defect wearing a different hat.
    """
    on_by_default = {
        name
        for name, svc in _TRACING_CLIENTS.items()
        if _default_of(_environment(svc)[_ENABLED]).lower() in ("1", "true")
    }
    if not on_by_default:
        pytest.skip("tracing is opt-in; an absent collector costs nothing")

    for host in (h for h in _COLLECTOR_HOSTS if h):
        profiles = set(_SERVICES[host].get("profiles") or [])
        assert profiles, (
            f"'{host}' has no profile, so it starts for everyone -- either give it "
            f"the monitoring profile or turn {_ENABLED} back off by default"
        )
