"""Transaction-join behavior for :class:`DatabaseOperationExecutor`.

Regression coverage for the double-begin bug: ``async_execute_transaction``
used to call ``session.begin()`` unconditionally on a caller-supplied session,
which raises ``InvalidRequestError`` when that session already autobegan a
transaction (SQLAlchemy autobegins on the first statement). The fake session
below reproduces that autobegin semantics so the old code path would raise here.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import InvalidRequestError

from app.db.runtime.operation_executor import DatabaseOperationExecutor


class _FakeTxContext:
    def __init__(self, session: _FakeAsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTxContext:
        self._session.in_tx = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is None:
            self._session.committed += 1
        else:
            self._session.rolled_back += 1
        self._session.in_tx = False
        return False


class _FakeAsyncSession:
    """Minimal AsyncSession stand-in mirroring autobegin/begin semantics."""

    def __init__(self, *, in_transaction: bool = False) -> None:
        self.in_tx = in_transaction
        self.begin_calls = 0
        self.committed = 0
        self.rolled_back = 0

    def in_transaction(self) -> bool:
        return self.in_tx

    def begin(self) -> _FakeTxContext:
        if self.in_tx:
            # SQLAlchemy raises exactly this when a transaction is already active.
            raise InvalidRequestError("A transaction is already begun on this Session")
        self.begin_calls += 1
        return _FakeTxContext(self)

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _executor(session_maker: Any = None) -> DatabaseOperationExecutor:
    return DatabaseOperationExecutor(session_maker=session_maker)


@pytest.mark.asyncio
async def test_passed_session_already_in_transaction_joins_without_begin() -> None:
    session = _FakeAsyncSession(in_transaction=True)
    executor = _executor()

    def op(passed: _FakeAsyncSession, value: int) -> int:
        assert passed is session
        return value * 2

    result = await executor.async_execute_transaction(op, 21, session=session)

    assert result == 42
    # The executor must not open a second transaction on a session the caller owns.
    assert session.begin_calls == 0
    # And it must not commit/rollback the caller's transaction either.
    assert session.committed == 0
    assert session.rolled_back == 0


@pytest.mark.asyncio
async def test_passed_session_without_transaction_begins_and_commits() -> None:
    session = _FakeAsyncSession(in_transaction=False)
    executor = _executor()

    def op(passed: _FakeAsyncSession, value: int) -> int:
        # The operation runs inside the transaction the executor opened.
        assert passed.in_tx is True
        return value

    result = await executor.async_execute_transaction(op, 7, session=session)

    assert result == 7
    assert session.begin_calls == 1
    assert session.committed == 1
    assert session.rolled_back == 0


@pytest.mark.asyncio
async def test_passed_session_in_transaction_supports_awaitable_operation() -> None:
    session = _FakeAsyncSession(in_transaction=True)
    executor = _executor()

    async def op(passed: _FakeAsyncSession, value: int) -> int:
        assert passed is session
        return value + 1

    result = await executor.async_execute_transaction(op, 99, session=session)

    assert result == 100
    assert session.begin_calls == 0


@pytest.mark.asyncio
async def test_no_session_uses_maker_and_opens_transaction() -> None:
    created: list[_FakeAsyncSession] = []

    def maker() -> _FakeAsyncSession:
        session = _FakeAsyncSession(in_transaction=False)
        created.append(session)
        return session

    executor = _executor(session_maker=maker)

    def op(passed: _FakeAsyncSession, value: int) -> int:
        assert passed.in_tx is True
        return value

    result = await executor.async_execute_transaction(op, 5)

    assert result == 5
    assert len(created) == 1
    assert created[0].begin_calls == 1
    assert created[0].committed == 1
