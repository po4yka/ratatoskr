"""Every Python service that can import torch must cap its BLAS thread pools.

torch reads nproc, not the cgroup quota, so sentence-transformers sized its
compute pool from the Pi's 4 cores inside a 0.75-CPU container. Four threads do
no more work than one under that quota -- they only burn it in a fraction of each
CFS period, after which the kernel freezes the whole container, event loop
included. Measured on 2026-07-29: 98.7 s throttled against 109.1 s of CPU used,
10.1% of periods throttled, and event_loop_stalled warnings long enough to time
out the taskiq broker read.

The fix reached worker and mobile-api, then ratatoskr, and stopped there. The
three MCP services build an EmbeddingService too (app/di/mcp.py) inside a
0.20-CPU quota -- a tighter ratio than the one that caused the incident. They
were missed because they are profile-gated and off by default.

So this file does not hardcode the service list any more. An earlier version did,
and that is exactly how the gap survived a test written to prevent it: the list
was the incomplete answer, asserted against itself. The set is derived from the
compose commands and from which DI module each entrypoint uses, so a new service
that reaches the embedding stack fails here instead of drifting.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# The caps must be env vars, not torch.set_num_threads(): torch reads them at
# import time and EmbeddingService imports torch lazily, so a call in Python
# would land after the pool is already sized.
_CAPS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")

# Compose command fragment -> the DI module that entrypoint composes from.
_ENTRYPOINT_DI = {
    "python -m bot": "app/di/telegram.py",
    "app.cli.taskiq_worker": "app/di/tasks.py",
    "app.cli.api_server": "app/di/api.py",
    "app.cli.mcp_server": "app/di/mcp.py",
}

_EMBEDDING_FACTORY = "create_embedding_service"


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


def _environment(service: dict) -> dict[str, str]:
    return dict(item.split("=", 1) for item in service.get("environment", []) if "=" in item)


def _reaches_embedding_factory(entry: str) -> bool:
    """Whether a DI module reaches create_embedding_service through app imports."""
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
        if _EMBEDDING_FACTORY in source:
            return True
        for module in re.findall(r"from (app\.[\w.]+) import", source):
            queue.append(module.replace(".", "/") + ".py")
    return False


def _services_needing_caps() -> dict[str, str]:
    """Compose service -> the DI module that pulls in the embedding stack."""
    base = _compose("docker-compose.yml")["services"]
    needing: dict[str, str] = {}
    for name, svc in base.items():
        command = svc.get("command")
        command_text = " ".join(command) if isinstance(command, list) else str(command or "")
        for fragment, di_module in _ENTRYPOINT_DI.items():
            if fragment in command_text and _reaches_embedding_factory(di_module):
                needing[name] = di_module
                break
    return needing


_NEEDING = _services_needing_caps()


def test_the_derivation_found_the_services() -> None:
    """A derivation that matches nothing would make every assertion below vacuous."""
    assert len(_NEEDING) >= 4, f"only found {sorted(_NEEDING)} -- the mapping has gone stale"
    for expected in ("ratatoskr", "worker", "mobile-api", "mcp"):
        assert expected in _NEEDING, f"{expected} builds an EmbeddingService and was not derived"


@pytest.mark.parametrize("service", sorted(_NEEDING))
@pytest.mark.parametrize("cap", _CAPS)
def test_the_cap_is_set(service: str, cap: str) -> None:
    env = _environment(_compose("docker-compose.pi.yml")["services"][service])
    assert cap in env, (
        f"{service} composes from {_NEEDING[service]}, which reaches "
        f"{_EMBEDDING_FACTORY}, but does not cap {cap}"
    )


@pytest.mark.parametrize("service", sorted(_NEEDING))
@pytest.mark.parametrize("cap", _CAPS)
def test_the_cap_defaults_to_one(service: str, cap: str) -> None:
    """An operator may raise it, but the shipped default has to be 1."""
    value = _environment(_compose("docker-compose.pi.yml")["services"][service])[cap]
    assert value == f"${{{cap}:-1}}", f"{service} sets {cap}={value}, expected a default of 1"


def test_the_scheduler_is_exempt_because_it_only_enqueues() -> None:
    """Guards the exemption: if the scheduler ever composes services, it needs caps."""
    assert "scheduler" not in _NEEDING
