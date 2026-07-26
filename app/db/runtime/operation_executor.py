"""Database execution helpers for the SQLAlchemy/Postgres runtime."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DatabaseOperationExecutor:
    """Execute transitional repository callables with AsyncSession injection."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
        operation_timeout: float = 30.0,
        max_retries: int = 3,
        logger: Any | None = None,
        **_legacy_kwargs: Any,
    ) -> None:
        self._session_maker = session_maker
        self._operation_timeout = operation_timeout
        self._max_retries = max_retries
        self._logger = logger or get_logger(__name__)

    @property
    def database(self) -> async_sessionmaker[AsyncSession] | None:
        return self._session_maker

    def connection_context(self) -> Any:
        msg = "connection_context is not available on the SQLAlchemy async runtime"
        raise RuntimeError(msg)

    async def async_execute(
        self,
        operation: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        operation_name: str = "repository_operation",
        read_only: bool = False,
        session: AsyncSession | None = None,
        **kwargs: Any,
    ) -> Any:
        del read_only
        effective_timeout = timeout if timeout is not None else self._operation_timeout

        async def _run() -> Any:
            return await self._execute(operation, session=session, args=args, kwargs=kwargs)

        try:
            retrying = _with_serialization_retry(_run, attempts=self._max_retries)
            return await asyncio.wait_for(retrying(), timeout=effective_timeout)
        except TimeoutError:
            self._logger.exception(
                "db_operation_timeout",
                extra={"operation": operation_name, "timeout": effective_timeout},
            )
            raise

    async def async_execute_transaction(
        self,
        operation: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        operation_name: str = "repository_transaction",
        session: AsyncSession | None = None,
        **kwargs: Any,
    ) -> Any:
        effective_timeout = timeout if timeout is not None else self._operation_timeout

        async def _run() -> Any:
            if session is not None:
                # A caller-supplied session may already own a transaction: SQLAlchemy
                # autobegins one on the first statement, and `session.begin()` raises
                # InvalidRequestError ("a transaction is already begun") when one is
                # active. Join the existing transaction in that case -- the caller owns
                # its commit/rollback lifecycle, so we must not open (or close) a second
                # one. Only begin ourselves when the session is transaction-free.
                if session.in_transaction():
                    return await self._call(operation, session, args, kwargs)
                async with session.begin():
                    return await self._call(operation, session, args, kwargs)
            maker = self._require_session_maker()
            async with maker() as new_session, new_session.begin():
                return await self._call(operation, new_session, args, kwargs)

        try:
            retrying = _with_serialization_retry(_run, attempts=self._max_retries)
            return await asyncio.wait_for(retrying(), timeout=effective_timeout)
        except TimeoutError:
            self._logger.exception(
                "db_transaction_timeout",
                extra={"operation": operation_name, "timeout": effective_timeout},
            )
            raise

    async def _execute(
        self,
        operation: Callable[..., Any],
        *,
        session: AsyncSession | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if session is not None:
            return await self._call(operation, session, args, kwargs)
        maker = self._require_session_maker()
        async with maker() as new_session:
            return await self._call(operation, new_session, args, kwargs)

    async def _call(
        self,
        operation: Callable[..., Any],
        session: AsyncSession,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        result = operation(session, *args, **kwargs)
        if _is_awaitable(result):
            return await result
        return result

    def _require_session_maker(self) -> async_sessionmaker[AsyncSession]:
        if self._session_maker is None:
            msg = "DatabaseOperationExecutor requires a SQLAlchemy session_maker"
            raise RuntimeError(msg)
        return self._session_maker


def _is_awaitable(value: Any) -> bool:
    return hasattr(value, "__await__")


def _sqlstate(exc: OperationalError) -> str | None:
    original = exc.orig
    for attr_name in ("sqlstate", "pgcode"):
        value = getattr(original, attr_name, None)
        if value:
            return str(value)
    return None


def _is_retryable_serialization_error(exc: OperationalError) -> bool:
    return _sqlstate(exc) in {"40001", "40P01"}


def _with_serialization_retry(
    func: Callable[[], Awaitable[Any]],
    *,
    attempts: int,
) -> Callable[[], Awaitable[Any]]:
    async def wrapper() -> Any:
        for attempt in range(1, attempts + 1):
            try:
                return await func()
            except OperationalError as exc:
                if not _is_retryable_serialization_error(exc) or attempt >= attempts:
                    raise
                await asyncio.sleep(0.05 * (2 ** (attempt - 1)))
        msg = "serialization retry requires at least one attempt"
        raise ValueError(msg)

    return wrapper
