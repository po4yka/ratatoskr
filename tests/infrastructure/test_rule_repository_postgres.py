"""Postgres-backed tests for the rule repository."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.db.models import AutomationRule, RuleExecutionLog, User
from app.db.session import Database
from app.infrastructure.persistence.repositories.rule_repository import (
    RuleRepositoryAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    await _clear(db)
    async with db.transaction() as session:
        session.add(User(telegram_user_id=15001, username="rules"))
    try:
        yield db
    finally:
        await _clear(db)
        await db.dispose()


async def _clear(database: Database) -> None:
    async with database.transaction() as session:
        await session.execute(delete(RuleExecutionLog))
        await session.execute(delete(AutomationRule))
        await session.execute(delete(User))


@pytest.mark.asyncio
async def test_rule_repository_crud_filters_and_soft_delete(database: Database) -> None:
    repo = RuleRepositoryAdapter(database)

    low = await repo.async_create_rule(
        15001,
        name="low",
        event_type="summary.created",
        conditions=[{"field": "tag"}],
        actions=[{"type": "tag"}],
        priority=1,
    )
    high = await repo.async_create_rule(
        15001,
        name="high",
        event_type="summary.created",
        conditions=[],
        actions=[],
        priority=10,
    )
    await repo.async_update_rule(
        low["id"],
        15001,
        name="updated",
        enabled=False,
        conditions=[{"field": "updated"}],
        actions=[{"type": "updated"}],
        ignored="ignored",
    )

    assert (await repo.async_get_rule_by_id(low["id"]))["name"] == "updated"
    assert [row["id"] for row in await repo.async_get_user_rules(15001)] == [
        high["id"],
        low["id"],
    ]
    assert [row["id"] for row in await repo.async_get_user_rules(15001, enabled_only=True)] == [
        high["id"]
    ]
    assert [
        row["id"] for row in await repo.async_get_rules_by_event_type(15001, "summary.created")
    ] == [high["id"]]

    await repo.async_soft_delete_rule(high["id"], 15001)
    assert [row["id"] for row in await repo.async_get_user_rules(15001)] == [low["id"]]


@pytest.mark.asyncio
async def test_rule_repository_run_count_and_execution_logs(database: Database) -> None:
    repo = RuleRepositoryAdapter(database)
    rule = await repo.async_create_rule(
        15001,
        name="rule",
        event_type="summary.created",
        conditions=[],
        actions=[],
    )

    await repo.async_increment_run_count(rule["id"])
    await repo.async_increment_run_count(rule["id"])
    loaded = await repo.async_get_rule_by_id(rule["id"])
    assert loaded is not None
    assert loaded["run_count"] == 2
    assert loaded["last_triggered_at"] is not None

    log = await repo.async_create_execution_log(
        rule["id"],
        summary_id=None,
        event_type="summary.created",
        matched=True,
        conditions_result=[{"ok": True}],
        actions_taken=[{"type": "tag"}],
        duration_ms=12,
    )
    rows = await repo.async_get_execution_logs(rule["id"])
    assert [row["id"] for row in rows] == [log["id"]]
    assert rows[0]["conditions_result_json"] == [{"ok": True}]
    assert rows[0]["actions_taken_json"] == [{"type": "tag"}]
