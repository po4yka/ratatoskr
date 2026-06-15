"""LangGraph Postgres checkpointer runtime lifecycle manager.

Owns a **dedicated** psycopg3 ``AsyncConnectionPool`` and an
``AsyncPostgresSaver`` that persist LangGraph summarize-graph state between
nodes (ADR-0004). Designed to be driven from the FastAPI/bot lifespan: a startup
failure must not prevent the service from running (the checkpointer is optional).

Invariant 4 (ADR-0018): this pool is the ONLY sanctioned non-``Database``
Postgres connection in the process. It is psycopg3 (not asyncpg) because
``langgraph-checkpoint-postgres`` requires psycopg3, and it must NOT route
through ``app.db.session.Database``. langgraph / psycopg imports are lazy
(inside ``start()``) so this module stays importable in the default image,
which does not install the optional ``graph`` extra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config.langgraph import LangGraphCheckpointConfig

logger = get_logger(__name__)


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
        """Open the dedicated pool, create the schema, and run saver.setup()."""
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
        await pool.open(wait=True)
        self._pool = pool

        # Create the dedicated schema before setup() (search_path may point at a
        # not-yet-existing schema; CREATE SCHEMA is schema-name explicit).
        async with pool.connection() as conn:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        # strict_msgpack -> no pickle fallback (no arbitrary-module deserialization).
        serde = JsonPlusSerializer(pickle_fallback=not cp_cfg.strict_msgpack)
        saver = AsyncPostgresSaver(pool, serde=serde)
        await saver.setup()
        self._saver = saver

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
