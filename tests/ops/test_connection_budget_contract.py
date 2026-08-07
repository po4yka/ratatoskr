"""The fleet's Postgres pools must fit under the server's max_connections.

Every process that builds a ``Database`` opens its own SQLAlchemy pool against
the same Postgres. The budget is therefore per-process times processes, and
nobody had done that arithmetic: docs/vector-index-sync.md quoted a
single-process figure ("~10 connections... default 100 is fine") as if it were
the whole deployment, under a env-var name that does not exist.

The pool-holding services and the per-process ceiling are both derived -- from
the compose commands and from the shipped configuration -- so adding a service
or raising a pool size fails here instead of surfacing as "FATAL: sorry, too
many clients already" across every container at once.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# postgres:17-alpine compiled-in defaults; the compose file overrides neither.
_POSTGRES_DEFAULT_MAX_CONNECTIONS = 100
_SUPERUSER_RESERVED = 3

# Non-fleet consumers that must still be able to connect: pg_dump, the metrics
# exporter, and an operator running a CLI while the stack is up.
_OUT_OF_FLEET_ALLOWANCE = 10

# Compose command fragment -> whether that entrypoint builds a Database.
_ENTRYPOINT_OPENS_POOL = {
    "python -m bot": True,
    "app.cli.taskiq_worker": True,
    "app.cli.api_server": True,
    "app.cli.mcp_server": True,
    # The scheduler runs the taskiq scheduler binary and only enqueues.
    "taskiq scheduler": False,
}


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


def _pool_holding_services() -> dict[str, bool]:
    """Compose service -> whether it is profile-gated (i.e. off by default)."""
    services = {}
    for name, svc in _compose("docker-compose.yml")["services"].items():
        command = svc.get("command")
        command_text = " ".join(command) if isinstance(command, list) else str(command or "")
        for fragment, opens_pool in _ENTRYPOINT_OPENS_POOL.items():
            if fragment in command_text:
                if opens_pool and svc.get("restart") != "no":
                    services[name] = bool(svc.get("profiles"))
                break
    return services


def _configured_pool_ceiling() -> int:
    """pool_size + max_overflow as the shipped configuration actually resolves them."""
    yaml_text = (ROOT / "config/ratatoskr.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(yaml_text) or {}
    database = data.get("database") or {}

    from app.config.database import DatabaseConfig

    pool_size = database.get("pool_size", DatabaseConfig.model_fields["pool_size"].default)
    overflow = database.get("max_overflow", DatabaseConfig.model_fields["max_overflow"].default)
    return int(pool_size) + int(overflow)


_POOL_SERVICES = _pool_holding_services()
_PER_PROCESS = _configured_pool_ceiling()
_USABLE = _POSTGRES_DEFAULT_MAX_CONNECTIONS - _SUPERUSER_RESERVED - _OUT_OF_FLEET_ALLOWANCE


def test_the_derivation_found_the_services() -> None:
    """A derivation that matches nothing would make every assertion below vacuous."""
    assert len(_POOL_SERVICES) >= 4, f"only found {sorted(_POOL_SERVICES)} -- the scan is stale"
    for expected in ("ratatoskr", "worker", "mobile-api", "mcp"):
        assert expected in _POOL_SERVICES, f"{expected} opens a Database and was not derived"
    assert "scheduler" not in _POOL_SERVICES, (
        "the scheduler opened a pool; the budget must count it"
    )


def test_postgres_still_runs_on_the_assumed_default() -> None:
    """The budget below assumes the image default; an override would invalidate it."""
    postgres = _compose("docker-compose.yml")["services"]["postgres"]
    assert "max_connections" not in str(postgres.get("command") or ""), (
        "postgres now sets max_connections explicitly -- update "
        "_POSTGRES_DEFAULT_MAX_CONNECTIONS and docs/vector-index-sync.md to match"
    )
    assert re.match(r"postgres:17(\.|-|$)", str(postgres.get("image", ""))), (
        f"postgres image changed to {postgres.get('image')!r}; re-check its default max_connections"
    )


def test_every_topology_fits_under_max_connections() -> None:
    default_topology = sum(1 for gated in _POOL_SERVICES.values() if not gated)
    full_topology = len(_POOL_SERVICES)

    for label, processes in (
        ("default (docker compose up / make pi-deploy)", default_topology),
        ("full (make pi-deploy-all, all mcp profiles)", full_topology),
    ):
        ceiling = processes * _PER_PROCESS
        assert ceiling <= _USABLE, (
            f"{label}: {processes} processes x {_PER_PROCESS} connections = {ceiling}, "
            f"above the {_USABLE} usable of {_POSTGRES_DEFAULT_MAX_CONNECTIONS} "
            f"(after {_SUPERUSER_RESERVED} superuser-reserved and "
            f"{_OUT_OF_FLEET_ALLOWANCE} for pg_dump/exporter/CLI). Lower "
            "database.pool_size in config/ratatoskr.yaml or raise max_connections, "
            "and update the budget table in docs/vector-index-sync.md."
        )


def test_the_documented_budget_matches_the_derived_one() -> None:
    """The doc states concrete totals; drift between them and the code is the bug."""
    budget_doc = (ROOT / "docs/vector-index-sync.md").read_text(encoding="utf-8")
    default_topology = sum(1 for gated in _POOL_SERVICES.values() if not gated)
    full_topology = len(_POOL_SERVICES)

    for processes in (default_topology, full_topology):
        stated = f"{processes} x {_PER_PROCESS} = **{processes * _PER_PROCESS}**"
        assert stated in budget_doc, (
            f"docs/vector-index-sync.md no longer states '{stated}'; the connection "
            "budget section has drifted from the deployment it describes"
        )


def test_mcp_servers_honour_the_configured_pool_size() -> None:
    """The MCP runtime used to rebuild DatabaseConfig from code defaults.

    That made the three most resource-starved services (0.20 CPU, 256M) the only
    ones ignoring the operator's configured sizing, and silently broke the
    arithmetic above.
    """
    pytest.importorskip("app.di.mcp")
    from app.config.database import DatabaseConfig
    from app.di.mcp import build_mcp_runtime

    configured = DatabaseConfig(
        dsn="postgresql+asyncpg://u:p@configured/db", pool_size=3, max_overflow=1
    )

    class _Cfg:
        database = configured

    runtime = build_mcp_runtime(
        database_dsn="postgresql+asyncpg://u:p@override/db2",
        cfg=_Cfg(),  # type: ignore[arg-type]
    )

    resolved = runtime.database.config
    assert (resolved.pool_size, resolved.max_overflow) == (3, 1), (
        "the MCP runtime discarded the configured pool sizing and fell back to "
        "code defaults, so these services size their pools independently of the "
        "fleet budget"
    )
    assert resolved.dsn == "postgresql+asyncpg://u:p@override/db2"
