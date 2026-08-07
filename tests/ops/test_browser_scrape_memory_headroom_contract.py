"""Every container that can launch an in-container Chromium needs the same memory ceiling.

The base 1 GiB cap was hit exactly -- memcg OOM, exit 137 -- when two concurrent
scrapes (MAX_CONCURRENT_CALLS=2) each launched a Chromium alongside the resident
embedding model, orphaning the in-flight request. The Pi overlay answered that by
raising `ratatoskr` to 3 GiB.

Then the workload moved and the ceiling did not follow. `config/ratatoskr.yaml`
sets `url_worker_enqueue_enabled: true`, so single URLs are summarized by
`app.tasks.url_processing` in `worker` -- still on the base 1 GiB cap. `mobile-api`
runs the same pipeline in-process for API submissions on an even smaller 1024M.
The cap was attached to a service name; the thing it protects against is the
scraper chain, which three services compose.

So the set is derived from the compose commands and from which DI module each
entrypoint composes from, not listed here. A hardcoded list is how the gap
survived the first time -- see the same lesson in
test_blas_thread_cap_contract.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# The bot's incident cap. Every service that can launch the same browser gets it.
_REQUIRED_BYTES = 3 * 1024**3

# Compose command fragment -> the DI module that entrypoint composes from.
_ENTRYPOINT_DI = {
    "python -m bot": "app/di/telegram.py",
    "app.cli.taskiq_worker": "app/di/tasks.py",
    "app.cli.api_server": "app/di/api.py",
    "app.cli.mcp_server": "app/di/mcp.py",
}

# The factory that builds the chain whose Playwright/Crawlee rungs launch a real
# browser process inside the container.
_SCRAPER_FACTORY = "ContentScraperFactory"

_MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


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


def _parse_memory(value: str) -> int:
    match = re.fullmatch(r"(\d+)([bkmg]?)", str(value).strip().lower())
    if match is None:
        raise ValueError(f"Unparseable compose memory value: {value!r}")
    return int(match.group(1)) * _MEMORY_UNITS[match.group(2) or "b"]


def _memory_limit(service: dict) -> str | None:
    return service.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory")


def _reaches_scraper_factory(entry: str) -> bool:
    """Whether a DI module reaches ContentScraperFactory through app imports."""
    seen: set[str] = set()
    queue = [entry]
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = ROOT / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        if _SCRAPER_FACTORY in source:
            return True
        for module in re.findall(r"from (app\.[\w.]+) import", source):
            queue.append(module.replace(".", "/") + ".py")
    return False


def _services_launching_browsers() -> dict[str, str]:
    """Compose service -> the DI module that pulls in the scraper chain."""
    base = _compose("docker-compose.yml")["services"]
    launching: dict[str, str] = {}
    for name, svc in base.items():
        command = svc.get("command")
        command_text = " ".join(command) if isinstance(command, list) else str(command or "")
        for fragment, di_module in _ENTRYPOINT_DI.items():
            if fragment in command_text and _reaches_scraper_factory(di_module):
                launching[name] = di_module
                break
    return launching


_LAUNCHING = _services_launching_browsers()


def test_the_derivation_found_the_services() -> None:
    """A derivation that matches nothing would make every assertion below vacuous."""
    assert len(_LAUNCHING) >= 3, f"only found {sorted(_LAUNCHING)} -- the mapping has gone stale"
    for expected in ("ratatoskr", "worker", "mobile-api"):
        assert expected in _LAUNCHING, f"{expected} composes a scraper chain and was not derived"


@pytest.mark.parametrize("service", sorted(_LAUNCHING))
def test_pi_grants_browser_headroom(service: str) -> None:
    """The Pi overlay must lift every browser-capable service off the base cap."""
    limit = _memory_limit(_compose("docker-compose.pi.yml")["services"][service])
    assert limit is not None, (
        f"{service} composes from {_LAUNCHING[service]}, which reaches {_SCRAPER_FACTORY}, "
        "but the Pi overlay leaves it on the base memory cap that already OOM-killed"
    )
    assert _parse_memory(limit) >= _REQUIRED_BYTES, (
        f"{service} caps memory at {limit}, below the {_REQUIRED_BYTES // 1024**3}G "
        "headroom an in-container Chromium plus the resident embedding model needs"
    )


def test_the_mcp_services_are_exempt_because_they_compose_no_scraper() -> None:
    """Guards the exemption: if an MCP server ever scrapes, it needs the headroom too."""
    assert not _reaches_scraper_factory("app/di/mcp.py")
    for mcp_service in ("mcp", "mcp-write", "mcp-public"):
        assert mcp_service not in _LAUNCHING


def test_the_scheduler_is_exempt_because_it_only_enqueues() -> None:
    """Guards the exemption: if the scheduler ever runs tasks, it needs the headroom."""
    assert "scheduler" not in _LAUNCHING
