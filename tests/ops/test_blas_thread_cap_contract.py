"""Every Python service that imports torch must cap its BLAS thread pools.

torch reads nproc, not the cgroup quota, so sentence-transformers sized its
compute pool from the Pi's 4 cores inside a 0.75-CPU container. Four threads do
no more work than one under that quota -- they only burn it in a fraction of each
CFS period, after which the kernel freezes the whole container, event loop
included. Measured on 2026-07-29: 98.7 s throttled against 109.1 s of CPU used,
10.1% of periods throttled, and event_loop_stalled warnings long enough to time
out the taskiq broker read.

The fix went to worker and mobile-api and was missed on ratatoskr, which builds
the embedding service through the same path (di/telegram.py -> di/search.py ->
create_embedding_service). Nothing checked, so nothing said. This is that check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# The caps must be env vars, not torch.set_num_threads(): torch reads them at
# import time and EmbeddingService imports torch lazily, so a call in Python
# would land after the pool is already sized.
_CAPS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")

# Services that construct an EmbeddingService, and therefore import torch.
_EMBEDDING_SERVICES = ("ratatoskr", "worker", "mobile-api")


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


def _pi_overlay() -> dict:
    return yaml.load(
        (ROOT / "ops/docker/docker-compose.pi.yml").read_text(encoding="utf-8"),
        Loader=_ComposeLoader,
    )


def _environment(service: dict) -> dict[str, str]:
    return dict(item.split("=", 1) for item in service.get("environment", []))


@pytest.mark.parametrize("service", _EMBEDDING_SERVICES)
@pytest.mark.parametrize("cap", _CAPS)
def test_the_cap_is_set_on_every_embedding_service(service: str, cap: str) -> None:
    env = _environment(_pi_overlay()["services"][service])
    assert cap in env, f"{service} does not cap {cap}; torch will size its pool from nproc"


@pytest.mark.parametrize("service", _EMBEDDING_SERVICES)
@pytest.mark.parametrize("cap", _CAPS)
def test_the_cap_defaults_to_one(service: str, cap: str) -> None:
    """An operator may raise it, but the shipped default has to be 1."""
    value = _environment(_pi_overlay()["services"][service])[cap]
    assert value == f"${{{cap}:-1}}", f"{service} sets {cap}={value}, expected a default of 1"


def test_the_services_that_need_it_are_the_ones_that_embed() -> None:
    """Guards the list above against a service being added and forgotten.

    Any Python service under the same CPU quota that reaches
    create_embedding_service needs the cap. The DI entrypoints are the evidence:
    the bot through di/telegram.py, the API through di/api.py, the worker through
    di/tasks.py.
    """
    for module in ("app/di/telegram.py", "app/di/api.py", "app/di/tasks.py"):
        source = (ROOT / module).read_text(encoding="utf-8")
        assert "search" in source or "embedding" in source, (
            f"{module} no longer reaches the embedding stack -- revisit the service list"
        )
