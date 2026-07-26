"""LangGraph Postgres checkpointer runtime lifecycle manager.

Owns a **dedicated** psycopg3 ``AsyncConnectionPool`` and an
``AsyncPostgresSaver`` that persist LangGraph summarize-graph state between
nodes (ADR-0004). Designed to be driven from the FastAPI/bot lifespan: a startup
failure must not prevent the service from running (the checkpointer is optional).

Invariant 4 (ADR-0018): this pool is the ONLY sanctioned non-``Database``
Postgres connection in the process. It is psycopg3 (not asyncpg) because
``langgraph-checkpoint-postgres`` requires psycopg3, and it must NOT route
through ``app.db.session.Database``. LangGraph / psycopg imports remain local to
``start()`` so importing infrastructure does not initialize driver state.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.infrastructure.checkpointing.cleanup import prune_expired_checkpoints

if TYPE_CHECKING:
    from app.config.langgraph import LangGraphCheckpointConfig

logger = get_logger(__name__)

# Bounded wait for the dedicated pool to establish its first connection. The
# checkpointer is optional and failure-isolated, so a slow/unreachable Postgres
# must not stall service startup for the psycopg_pool default (30s).
_POOL_OPEN_TIMEOUT_SEC = 10.0
# Stable, process-independent PostgreSQL advisory lock key for LangGraph's
# non-transactional migration version check + insert sequence.
_SETUP_ADVISORY_LOCK_ID = 0x52415441544F534B


def _psycopg_dsn(database_dsn: str, dsn_override: str | None) -> str:
    """Return a psycopg3 DSN, stripping the asyncpg driver suffix.

    psycopg3 uses the bare ``postgresql://`` scheme; the application's
    ``DATABASE_URL`` carries the SQLAlchemy ``+asyncpg`` driver suffix.
    """
    dsn = dsn_override or database_dsn
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


class CheckpointerRuntime:
    """Manages the dedicated psycopg3 pool + AsyncPostgresSaver lifecycle."""

    def __init__(self, *, cfg: Any) -> None:
        # cfg is AppConfig -- typed as Any to avoid a circular import.
        self._cfg = cfg
        self._pool: Any | None = None
        self._saver: Any | None = None

    @property
    def saver(self) -> Any:
        """The AsyncPostgresSaver, available after ``start()``.

        The graph-compilation seam (T5) injects this as the checkpointer.
        """
        if self._saver is None:
            raise RuntimeError("CheckpointerRuntime.start() must be called before accessing saver")
        return self._saver

    async def start(self) -> None:
        """Open the pool and clean retained state before exposing a durable saver."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        cp_cfg: LangGraphCheckpointConfig = self._cfg.langgraph_checkpoint
        schema = cp_cfg.schema_name
        dsn = _psycopg_dsn(self._cfg.database.dsn, cp_cfg.dsn_override)

        async def _configure(conn: Any) -> None:
            # Pin every pooled connection to the dedicated checkpoint schema.
            await conn.execute(f'SET search_path TO "{schema}"')

        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=cp_cfg.pool_min_size,
            max_size=cp_cfg.pool_max_size,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
            configure=_configure,
            name="langgraph-checkpointer",
        )
        # Bounded open so a slow/unreachable Postgres cannot stall service
        # startup (the checkpointer is optional and failure-isolated).
        await pool.open(wait=True, timeout=_POOL_OPEN_TIMEOUT_SEC)
        self._pool = pool

        try:
            # Create the dedicated schema before setup() (the per-connection
            # search_path may point at a not-yet-existing schema; CREATE SCHEMA is
            # schema-name explicit). `schema` is validated to [A-Za-z0-9_] at
            # config time, so the interpolation is injection-safe.
            # ``AsyncPostgresSaver.setup()`` reads the current migration version
            # and then inserts subsequent versions. Serialize that sequence across
            # bot/API processes. Run setup on the lock-owning connection itself so
            # a pool configured with max_size=1 cannot deadlock waiting for a
            # second connection.
            async with pool.connection() as conn:
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                await conn.execute("SELECT pg_advisory_lock(%s)", (_SETUP_ADVISORY_LOCK_ID,))
                try:
                    # strict_msgpack -> no pickle fallback (no arbitrary-module
                    # deserialization). Production always forces it off.
                    allow_pickle = not cp_cfg.strict_msgpack
                    if allow_pickle and self._cfg.deployment.is_production_mode:
                        logger.warning("langgraph_pickle_fallback_disabled_in_production")
                        allow_pickle = False
                    serde = JsonPlusSerializer(pickle_fallback=allow_pickle)
                    setup_saver = AsyncPostgresSaver(conn, serde=serde)
                    await setup_saver.setup()
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_ADVISORY_LOCK_ID,))

            saver = AsyncPostgresSaver(pool, serde=serde)

            # setup() bootstraps the checkpoint tables on a fresh deployment. Only publish the saver after pruning old runs, so production graph compilation cannot enable durable execution ahead of cleanup.
            async with pool.connection() as conn, conn.transaction():
                prune_stats = await prune_expired_checkpoints(
                    conn,
                    schema=schema,
                    retention_days=cp_cfg.retention_days,
                )
            logger.info("langgraph_startup_checkpoint_cleanup_complete", extra=asdict(prune_stats))
            self._saver = saver
        except Exception:
            # Never leak the just-opened pool if schema creation / setup fails:
            # the caller may discard this instance on error (failure isolation).
            await self.stop()
            raise

        logger.info(
            "langgraph_checkpointer_ready",
            extra={
                "schema": schema,
                "pool_min": cp_cfg.pool_min_size,
                "pool_max": cp_cfg.pool_max_size,
                "strict_msgpack": cp_cfg.strict_msgpack,
            },
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """Close the dedicated pool (idempotent)."""
        self._saver = None
        pool = self._pool
        if pool is None:
            return
        self._pool = None
        try:
            await pool.close(timeout=timeout)
            logger.info("langgraph_checkpointer_stopped")
        except Exception:
            logger.exception("langgraph_checkpointer_stop_error")
