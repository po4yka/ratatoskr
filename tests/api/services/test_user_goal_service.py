from __future__ import annotations

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.models.requests import CreateGoalRequest
from app.api.services.user_goal_service import UserGoalService
from app.db.models import Collection, CollectionItem, SummaryTag, Tag


@pytest.mark.asyncio
async def test_user_goal_service_upserts_lists_and_deletes_goals(db, user_factory) -> None:
    user = await user_factory(telegram_user_id=7101, username="goal-user")
    service = UserGoalService(db)

    created = await service.upsert_goal(
        user_id=user.telegram_user_id,
        body=CreateGoalRequest(goal_type="daily", target_count=3),
    )
    assert created["goalType"] == "daily"
    assert created["targetCount"] == 3

    updated = await service.upsert_goal(
        user_id=user.telegram_user_id,
        body=CreateGoalRequest(goal_type="daily", target_count=5),
    )
    assert updated["id"] == created["id"]
    assert updated["targetCount"] == 5

    listed = await service.list_goals(user_id=user.telegram_user_id)
    assert len(listed) == 1
    assert listed[0]["targetCount"] == 5

    await service.delete_global_goal(user_id=user.telegram_user_id, goal_type="daily")
    assert await service.list_goals(user_id=user.telegram_user_id) == []


@pytest.mark.asyncio
async def test_user_goal_service_validates_scope_ownership_and_reports_progress(
    db, user_factory, summary_factory
) -> None:
    user = await user_factory(telegram_user_id=7102, username="scoped-goal-user")
    other = await user_factory(telegram_user_id=7103, username="other-user")
    service = UserGoalService(db)

    summary = await summary_factory(user=user)
    async with db.transaction() as session:
        tag = Tag(user_id=user.telegram_user_id, name="AI", normalized_name="ai")
        collection = Collection(user_id=user.telegram_user_id, name="Reading list")
        session.add_all([tag, collection])
        await session.flush()
        session.add_all(
            [
                SummaryTag(summary_id=summary.id, tag_id=tag.id),
                CollectionItem(collection_id=collection.id, summary_id=summary.id),
            ]
        )

    await service.upsert_goal(
        user_id=user.telegram_user_id,
        body=CreateGoalRequest(
            goal_type="daily", target_count=1, scope_type="tag", scope_id=tag.id
        ),
    )
    await service.upsert_goal(
        user_id=user.telegram_user_id,
        body=CreateGoalRequest(
            goal_type="daily",
            target_count=1,
            scope_type="collection",
            scope_id=collection.id,
        ),
    )
    await service.upsert_goal(
        user_id=user.telegram_user_id,
        body=CreateGoalRequest(goal_type="daily", target_count=1),
    )

    progress = await service.get_goal_progress(user_id=user.telegram_user_id)
    assert len(progress) == 3
    assert all(item["currentCount"] >= 1 for item in progress)
    assert all(item["achieved"] is True for item in progress)
    assert {item["scopeName"] for item in progress if item["scopeType"] != "global"} == {
        "AI",
        "Reading list",
    }

    async with db.transaction() as session:
        foreign_tag = Tag(
            user_id=other.telegram_user_id,
            name="Other",
            normalized_name="other",
        )
        session.add(foreign_tag)
        await session.flush()

    with pytest.raises(ResourceNotFoundError):
        await service.upsert_goal(
            user_id=user.telegram_user_id,
            body=CreateGoalRequest(
                goal_type="weekly",
                target_count=2,
                scope_type="tag",
                scope_id=foreign_tag.id,
            ),
        )
