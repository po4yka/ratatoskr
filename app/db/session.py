"""SQLAlchemy async database session management."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.logging_utils import get_logger
from app.db.runtime.backup import DatabaseBackupService
from app.db.runtime.bootstrap import DatabaseBootstrapService
from app.db.runtime.inspection import DatabaseInspectionService
from app.db.runtime.maintenance import DatabaseMaintenanceService
from app.db.runtime.operation_executor import DatabaseOperationExecutor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
    from pathlib import Path

    from app.config.database import DatabaseConfig

logger = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

_RETRYABLE_SQLSTATES = {"40001", "40P01"}


@dataclass(slots=True)
class Database:
    """Async SQLAlchemy database facade for bot, CLI, and FastAPI callers."""

    config: DatabaseConfig
    _engine: AsyncEngine = field(init=False, repr=False)
    _session_maker: async_sessionmaker[AsyncSession] = field(init=False, repr=False)
    _bootstrap: DatabaseBootstrapService = field(init=False, repr=False)
    _executor: DatabaseOperationExecutor = field(init=False, repr=False)
    _maintenance: DatabaseMaintenanceService = field(init=False, repr=False)
    _inspection: DatabaseInspectionService = field(init=False, repr=False)
    _backup: DatabaseBackupService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        engine_kwargs: dict[str, Any] = {
            "pool_pre_ping": True,
            # asyncpg prepared-statement cache size (per connection). Configurable
            # so it can be set to 0 to avoid "cached plan must not change result
            # type" errors under a pooling proxy or varying IN-list churn.
            "connect_args": {
                "prepared_statement_cache_size": self.config.prepared_statement_cache_size,
            },
        }
        if os.getenv("RATATOSKR_DATABASE_NULL_POOL") == "1":
            engine_kwargs["poolclass"] = NullPool
        else:
            engine_kwargs.update(
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
                pool_recycle=self.config.pool_recycle_seconds,
                pool_timeout=self.config.pool_timeout_seconds,
            )
        self._engine = create_async_engine(self.config.dsn, **engine_kwargs)
        self._session_maker = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        self._bootstrap = DatabaseBootstrapService(dsn=self.config.dsn, logger=logger)
        self._executor = DatabaseOperationExecutor(
            session_maker=self._session_maker,
            operation_timeout=self.config.operation_timeout,
            max_retries=self.config.max_retries,
            logger=logger,
        )
        self._maintenance = DatabaseMaintenanceService(
            engine=self._engine,
            session_maker=self._session_maker,
            logger=logger,
        )
        self._inspection = DatabaseInspectionService(
            session_maker=self._session_maker,
            logger=logger,
        )
        self._backup = DatabaseBackupService(dsn=self.config.dsn, logger=logger)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_maker(self) -> async_sessionmaker[AsyncSession]:
        return self._session_maker

    @property
    def database(self) -> async_sessionmaker[AsyncSession]:
        """Compatibility property for callers moving off Peewee."""
        return self._session_maker

    @property
    def executor(self) -> DatabaseOperationExecutor:
        return self._executor

    @property
    def bootstrap(self) -> DatabaseBootstrapService:
        return self._bootstrap

    @property
    def maintenance(self) -> DatabaseMaintenanceService:
        return self._maintenance

    @property
    def inspection(self) -> DatabaseInspectionService:
        return self._inspection

    @property
    def backups(self) -> DatabaseBackupService:
        return self._backup

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session without starting an implicit transaction block."""
        async with self._session_maker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield a session inside a transaction, committing only on success."""
        async with self._session_maker() as session, session.begin():
            yield session

    async def healthcheck(self) -> None:
        async with self.session() as session:
            await session.execute(text("SELECT 1"))

    async def migrate(self) -> None:
        """Run Alembic migrations for the configured database."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._bootstrap.migrate)
        self._maintenance.run_startup_maintenance()

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def async_execute(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._executor.async_execute(operation, *args, **kwargs)

    async def async_execute_transaction(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        return await self._executor.async_execute_transaction(operation, *args, **kwargs)

    def create_backup_copy(self, dest_path: str) -> Path:
        return self._backup.create_backup_copy(dest_path)

    def check_integrity(self) -> tuple[bool, str]:
        return self._inspection.check_integrity()

    def get_database_overview(self) -> dict[str, object]:
        return self._inspection.get_database_overview()

    def verify_processing_integrity(
        self,
        *,
        required_fields: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        return self._inspection.verify_processing_integrity(
            required_fields=required_fields,
            limit=limit,
        )


@asynccontextmanager
async def get_session(database: Database) -> AsyncIterator[AsyncSession]:
    """Open a short-lived bot/CLI session."""
    async with database.session() as session:
        yield session


async def get_session_for_request(database: Database) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that wraps each request in one transaction."""
    async with database.transaction() as session:
        yield session


def _sqlstate(exc: OperationalError) -> str | None:
    original = exc.orig
    for attr_name in ("sqlstate", "pgcode"):
        value = getattr(original, attr_name, None)
        if value:
            return str(value)
    return None


def _is_retryable_serialization_error(exc: OperationalError) -> bool:
    return _sqlstate(exc) in _RETRYABLE_SQLSTATES


def with_serialization_retry(
    func: Callable[P, Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.05,
) -> Callable[P, Awaitable[T]]:
    """Retry an async operation on Postgres serialization/deadlock failures."""

    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        last_error: OperationalError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await func(*args, **kwargs)
            except OperationalError as exc:
                if not _is_retryable_serialization_error(exc) or attempt >= attempts:
                    raise
                last_error = exc
                delay = base_delay_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "db_serialization_retry",
                    extra={"attempt": attempt, "sqlstate": _sqlstate(exc), "delay": delay},
                )
                await asyncio.sleep(delay)
        if last_error is not None:  # pragma: no cover - loop exits by raise/return.
            raise last_error
        msg = "with_serialization_retry requires at least one attempt"
        raise ValueError(msg)

    return wrapper
