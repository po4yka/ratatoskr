"""Service logic for reading goal endpoints."""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.api.dependencies.database import get_session_manager, get_summary_repository
from app.api.exceptions import ResourceNotFoundError
from app.api.models.responses import GoalProgressResponse, GoalResponse
from app.core.time_utils import UTC
from app.db.session import Database  # noqa: TC001  # used at runtime in __init__ signature
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.api.models.requests import CreateGoalRequest


def _safe_isoformat(dt_value: Any) -> str | None:
    if dt_value is None:
        return None
    if hasattr(dt_value, "isoformat") and not isinstance(dt_value, str):
        return str(dt_value.isoformat()) + "Z"
    if isinstance(dt_value, str):
        try:
            parsed = datetime.fromisoformat(dt_value.replace("Z", "+00:00"))
            return parsed.isoformat() + "Z"
        except (ValueError, AttributeError):
            return dt_value if dt_value else None
    return None


class UserGoalService:
    """Owns goal persistence, scope validation, and progress calculations."""

    def __init__(self, session_manager: Database | None = None) -> None:
        self._db = session_manager or get_session_manager()
        self._user_content_repo = UserContentRepositoryAdapter(self._db)
        self._summary_repo = get_summary_repository(self._db)

    async def list_goals(self, *, user_id: int) -> list[dict[str, Any]]:
        """List all goals for a user."""
        goals = await self._user_content_repo.async_list_goals(user_id)
        return [await self._goal_to_payload(goal, user_id=user_id) for goal in goals]

    async def upsert_goal(self, *, user_id: int, body: CreateGoalRequest) -> dict[str, Any]:
        """Create or update a scoped reading goal."""
        await self._validate_scope_ownership(
            user_id=user_id,
            scope_type=body.scope_type,
            scope_id=body.scope_id,
        )
        goal = await self._user_content_repo.async_upsert_goal(
            user_id=user_id,
            goal_type=body.goal_type,
            scope_type=body.scope_type,
            scope_id=body.scope_id,
            target_count=body.target_count,
        )
        return await self._goal_to_payload(goal, user_id=user_id)

    async def delete_global_goal(self, *, user_id: int, goal_type: str) -> None:
        """Delete a global goal by type."""
        deleted_count = await self._user_content_repo.async_delete_global_goal(
            user_id=user_id, goal_type=goal_type
        )
        if deleted_count == 0:
            raise ResourceNotFoundError("Goal", goal_type)

    async def delete_goal_by_id(self, *, user_id: int, goal_id: str) -> None:
        """Delete a goal by its UUID."""
        deleted_count = await self._user_content_repo.async_delete_goal_by_id(
            user_id=user_id, goal_id=goal_id
        )
        if deleted_count == 0:
            raise ResourceNotFoundError("Goal", goal_id)

    async def get_goal_progress(self, *, user_id: int) -> list[dict[str, Any]]:
        """Return progress for each goal."""
        goals = await self._user_content_repo.async_list_goals(user_id)
        if not goals:
            return []

        streak_data = await self._compute_streak_data(user_id=user_id)
        now = datetime.now(UTC)
        today = now.date()
        progress_list: list[dict[str, Any]] = []

        for goal in goals:
            scope_type = str(goal.get("scope_type") or "global")
            scope_id = goal.get("scope_id")
            goal_type = str(goal.get("goal_type") or "daily")
            target_count = int(goal.get("target_count") or 0)

            if scope_type == "global":
                current_count = self._global_goal_count(
                    goal_type=goal_type, streak_data=streak_data
                )
            else:
                start, end = self._goal_period_bounds(goal_type=goal_type, today=today)
                current_count = (
                    await self._user_content_repo.async_count_scoped_summaries_in_period(
                        user_id=user_id,
                        start=start,
                        end=end,
                        scope_type=scope_type,
                        scope_id=scope_id,
                    )
                )

            progress_list.append(
                GoalProgressResponse(
                    goal_type=goal_type,
                    target_count=target_count,
                    current_count=current_count,
                    achieved=current_count >= target_count,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    scope_name=await self._resolve_scope_name(
                        user_id=user_id,
                        scope_type=scope_type,
                        scope_id=scope_id,
                    ),
                ).model_dump(by_alias=True)
            )

        return progress_list

    async def _validate_scope_ownership(
        self, *, user_id: int, scope_type: str, scope_id: int | None
    ) -> None:
        if scope_type in {"tag", "collection"} and scope_id is not None:
            scope_name = await self._user_content_repo.async_get_scope_name(
                user_id=user_id,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            if scope_name is None:
                raise ResourceNotFoundError(scope_type.title(), str(scope_id))

    async def _resolve_scope_name(
        self, *, user_id: int, scope_type: str, scope_id: int | None
    ) -> str | None:
        return await self._user_content_repo.async_get_scope_name(
            user_id=user_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )

    async def _goal_to_payload(self, goal: Any, *, user_id: int) -> dict[str, Any]:
        return GoalResponse(
            id=str(goal.get("id")),
            goal_type=str(goal.get("goal_type") or ""),
            target_count=int(goal.get("target_count") or 0),
            scope_type=str(goal.get("scope_type") or "global"),
            scope_id=goal.get("scope_id"),
            scope_name=await self._resolve_scope_name(
                user_id=user_id,
                scope_type=str(goal.get("scope_type") or "global"),
                scope_id=goal.get("scope_id"),
            ),
            created_at=_safe_isoformat(goal.get("created_at")) or "",
            updated_at=_safe_isoformat(goal.get("updated_at")) or "",
        ).model_dump(by_alias=True)

    @staticmethod
    def _goal_period_bounds(*, goal_type: str, today: _dt.date) -> tuple[datetime, datetime]:
        if goal_type == "daily":
            start = datetime(today.year, today.month, today.day, tzinfo=UTC)
            end = start + timedelta(days=1)
            return start, end
        if goal_type == "weekly":
            start_of_week = today - timedelta(days=today.weekday())
            start = datetime(start_of_week.year, start_of_week.month, start_of_week.day, tzinfo=UTC)
            return start, start + timedelta(days=7)
        start = datetime(today.year, today.month, 1, tzinfo=UTC)
        if today.month == 12:
            end = datetime(today.year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(today.year, today.month + 1, 1, tzinfo=UTC)
        return start, end

    @staticmethod
    def _global_goal_count(*, goal_type: str, streak_data: dict[str, Any]) -> int:
        if goal_type == "daily":
            return int(streak_data["today_count"])
        if goal_type == "weekly":
            return int(streak_data["week_count"])
        if goal_type == "monthly":
            return int(streak_data["month_count"])
        return 0

    async def _compute_streak_data(self, *, user_id: int) -> dict[str, Any]:
        now = datetime.now(UTC)
        today = now.date()
        cutoff = now - timedelta(days=365)
        rows = await self._summary_repo.async_get_user_summary_activity_dates(user_id, cutoff)

        active_dates: set[_dt.date] = set()
        today_count = 0
        start_of_week = today - timedelta(days=today.weekday())
        week_count = 0
        start_of_month = today.replace(day=1)
        month_count = 0

        for created in rows:
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if not hasattr(created, "date"):
                continue
            created_date = created.date()
            active_dates.add(created_date)
            if created_date == today:
                today_count += 1
            if created_date >= start_of_week:
                week_count += 1
            if created_date >= start_of_month:
                month_count += 1

        if not active_dates:
            return {
                "current_streak": 0,
                "longest_streak": 0,
                "last_activity_date": None,
                "today_count": 0,
                "week_count": 0,
                "month_count": 0,
            }

        sorted_dates = sorted(active_dates, reverse=True)
        current_streak = 0
        check_date: _dt.date | None = today
        if check_date not in active_dates:
            yesterday = today - timedelta(days=1)
            check_date = yesterday if yesterday in active_dates else None

        if check_date is not None:
            while check_date in active_dates:
                current_streak += 1
                check_date -= timedelta(days=1)

        longest_streak = 0
        streak = 1
        for index in range(1, len(sorted_dates)):
            if sorted_dates[index] == sorted_dates[index - 1] - timedelta(days=1):
                streak += 1
            else:
                longest_streak = max(longest_streak, streak)
                streak = 1
        longest_streak = max(longest_streak, streak)

        return {
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "last_activity_date": sorted_dates[0].isoformat(),
            "today_count": today_count,
            "week_count": week_count,
            "month_count": month_count,
        }
