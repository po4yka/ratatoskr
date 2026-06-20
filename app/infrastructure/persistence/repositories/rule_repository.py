"""SQLAlchemy implementation of the rule repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from app.db.models import AutomationRule, RuleExecutionLog, model_to_dict
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class RuleRepositoryAdapter:
    """Adapter for automation rule CRUD and execution log operations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_user_rules(
        self, user_id: int, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return all non-deleted rules owned by a user."""
        async with self._database.session() as session:
            stmt = select(AutomationRule).where(
                AutomationRule.user_id == user_id,
                AutomationRule.is_deleted.is_(False),
            )
            if enabled_only:
                stmt = stmt.where(AutomationRule.enabled.is_(True))
            rows = (
                await session.execute(
                    stmt.order_by(AutomationRule.priority.desc(), AutomationRule.created_at.asc())
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_get_rule_by_id(self, rule_id: int) -> dict[str, Any] | None:
        """Return rule by ID."""
        async with self._database.session() as session:
            rule = await session.get(AutomationRule, rule_id)
            return model_to_dict(rule)

    async def async_get_rules_by_event_type(
        self, user_id: int, event_type: str
    ) -> list[dict[str, Any]]:
        """Return enabled rules matching event type, ordered by priority."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(AutomationRule)
                    .where(
                        AutomationRule.user_id == user_id,
                        AutomationRule.event_type == event_type,
                        AutomationRule.enabled.is_(True),
                        AutomationRule.is_deleted.is_(False),
                    )
                    .order_by(AutomationRule.priority.desc())
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_create_rule(
        self,
        user_id: int,
        name: str,
        event_type: str,
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        match_mode: str = "all",
        priority: int = 0,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a rule and return the created record."""
        async with self._database.transaction() as session:
            rule = AutomationRule(
                user_id=user_id,
                name=name,
                event_type=event_type,
                conditions_json=conditions,
                actions_json=actions,
                match_mode=match_mode,
                priority=priority,
                description=description,
            )
            session.add(rule)
            await session.flush()
            return model_to_dict(rule) or {}

    async def async_update_rule(self, rule_id: int, user_id: int, **fields: Any) -> dict[str, Any]:
        """Update provided fields on a rule and return the updated record."""
        field_map = {
            "name": "name",
            "description": "description",
            "enabled": "enabled",
            "event_type": "event_type",
            "match_mode": "match_mode",
            "conditions": "conditions_json",
            "actions": "actions_json",
            "priority": "priority",
        }
        update_values = {attr: fields[key] for key, attr in field_map.items() if key in fields}
        update_values["updated_at"] = _utcnow()

        async with self._database.transaction() as session:
            await session.execute(
                update(AutomationRule)
                .where(AutomationRule.id == rule_id, AutomationRule.user_id == user_id)
                .values(**update_values)
            )
            rule = await session.get(AutomationRule, rule_id)
            return model_to_dict(rule) or {}

    async def async_soft_delete_rule(self, rule_id: int, user_id: int) -> None:
        """Soft-delete a rule owned by user_id."""
        async with self._database.transaction() as session:
            await session.execute(
                update(AutomationRule)
                .where(AutomationRule.id == rule_id, AutomationRule.user_id == user_id)
                .values(is_deleted=True, deleted_at=_utcnow(), updated_at=_utcnow())
            )

    async def async_increment_run_count(self, rule_id: int) -> None:
        """Increment run_count and set last_triggered_at to now."""
        async with self._database.transaction() as session:
            await session.execute(
                update(AutomationRule)
                .where(AutomationRule.id == rule_id)
                .values(
                    run_count=AutomationRule.run_count + 1,
                    last_triggered_at=_utcnow(),
                    updated_at=_utcnow(),
                )
            )

    async def async_create_execution_log(
        self,
        rule_id: int,
        summary_id: int | None,
        event_type: str,
        matched: bool,
        conditions_result: list[dict[str, Any]] | None = None,
        actions_taken: list[dict[str, Any]] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Insert an execution log entry and return the created record."""
        async with self._database.transaction() as session:
            log = RuleExecutionLog(
                rule_id=rule_id,
                summary_id=summary_id,
                event_type=event_type,
                matched=matched,
                conditions_result_json=conditions_result,
                actions_taken_json=actions_taken,
                error=error,
                duration_ms=duration_ms,
            )
            session.add(log)
            await session.flush()
            return model_to_dict(log) or {}

    async def async_get_execution_logs(
        self, rule_id: int, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return paginated execution logs for a rule."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(RuleExecutionLog)
                    .where(RuleExecutionLog.rule_id == rule_id)
                    .order_by(RuleExecutionLog.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]
