from __future__ import annotations

import uuid

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.models.requests import CreateHighlightRequest, UpdateHighlightRequest
from app.api.services.highlight_service import SummaryHighlightService
from app.db.models import SummaryHighlight


@pytest.mark.asyncio
async def test_highlight_service_crud_round_trip(db, user_factory, summary_factory) -> None:
    user = await user_factory(telegram_user_id=7001, username="highlight-user")
    summary = await summary_factory(user=user)
    service = SummaryHighlightService(db)

    created = await service.create_highlight(
        user_id=user.telegram_user_id,
        summary_id=summary.id,
        body=CreateHighlightRequest(text="Important text", color="yellow"),
    )

    listed = await service.list_highlights(user_id=user.telegram_user_id, summary_id=summary.id)
    assert len(listed) == 1
    assert listed[0]["text"] == "Important text"
    assert created["id"] == listed[0]["id"]

    updated = await service.update_highlight(
        user_id=user.telegram_user_id,
        summary_id=summary.id,
        highlight_id=created["id"],
        body=UpdateHighlightRequest(color="blue", note="revisit"),
    )
    assert updated["color"] == "blue"
    assert updated["note"] == "revisit"

    await service.delete_highlight(
        user_id=user.telegram_user_id,
        summary_id=summary.id,
        highlight_id=created["id"],
    )
    assert await service.list_highlights(user_id=user.telegram_user_id, summary_id=summary.id) == []


@pytest.mark.asyncio
async def test_highlight_service_rejects_unowned_summary_and_missing_highlight(
    db, user_factory, summary_factory
) -> None:
    owner = await user_factory(telegram_user_id=7002, username="owner")
    other = await user_factory(telegram_user_id=7003, username="other")
    summary = await summary_factory(user=owner)
    service = SummaryHighlightService(db)

    with pytest.raises(ResourceNotFoundError):
        await service.list_highlights(user_id=other.telegram_user_id, summary_id=summary.id)

    async with db.transaction() as session:
        session.add(
            SummaryHighlight(
                id=uuid.uuid4(),
                user_id=owner.telegram_user_id,
                summary_id=summary.id,
                text="Owned highlight",
            )
        )

    with pytest.raises(ResourceNotFoundError):
        await service.update_highlight(
            user_id=owner.telegram_user_id,
            summary_id=summary.id,
            highlight_id=str(uuid.uuid4()),
            body=UpdateHighlightRequest(note="missing"),
        )
