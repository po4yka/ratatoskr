"""
Tests for favorites (direct calls).
"""

import pytest
from sqlalchemy import select

from app.api.routers import summaries
from app.db.models import Request, Summary


@pytest.mark.asyncio
async def test_toggle_favorite(db, user_factory):
    user = await user_factory(username="fav_user")
    user_context = {"user_id": user.telegram_user_id}

    # Manually create summary
    async with db.transaction() as session:
        req = Request(
            user_id=user.telegram_user_id,
            input_url="http://test1.com",
            normalized_url="http://test1.com",
            status="completed",
            type="url",
        )
        session.add(req)
        await session.flush()
        summary = Summary(
            request_id=req.id,
            lang="en",
            is_read=False,
            version=1,
            json_payload={},
        )
        session.add(summary)
        await session.flush()

    assert not summary.is_favorited

    use_case = summaries._get_summary_use_case()

    # Toggle ON
    response = await summaries.toggle_favorite(
        summary_id=summary.id, user=user_context, use_case=use_case
    )
    assert response["success"] is True
    assert response["data"]["isFavorited"] is True

    async with db.session() as session:
        summary = await session.scalar(select(Summary).where(Summary.id == summary.id))
    assert summary is not None
    assert summary.is_favorited is True

    # Toggle OFF
    response = await summaries.toggle_favorite(
        summary_id=summary.id, user=user_context, use_case=use_case
    )
    assert response["data"]["isFavorited"] is False

    async with db.session() as session:
        summary = await session.scalar(select(Summary).where(Summary.id == summary.id))
    assert summary is not None
    assert summary.is_favorited is False


@pytest.mark.asyncio
async def test_get_summaries_filter(db, user_factory, summary_factory):
    user = await user_factory(username="fav_user_filter")
    user_context = {"user_id": user.telegram_user_id}

    # S1: Favorited
    s1 = await summary_factory(user=user)
    s1.is_favorited = True
    async with db.transaction() as session:
        await session.merge(s1)

    # S2: Not Favorited
    s2 = await summary_factory(user=user)

    use_case = summaries._get_summary_use_case()

    # All
    resp = await summaries.get_summaries(
        user=user_context,
        limit=20,
        offset=0,
        sort="created_at_desc",
        is_read=None,
        is_favorited=None,
        lang=None,
        start_date=None,
        end_date=None,
        search=None,
        use_case=use_case,
    )
    data = resp["data"]["summaries"]
    ids = [s["id"] for s in data]
    assert s1.id in ids
    assert s2.id in ids

    # Favorites only
    resp = await summaries.get_summaries(
        user=user_context,
        limit=20,
        offset=0,
        sort="created_at_desc",
        is_read=None,
        is_favorited=True,
        lang=None,
        start_date=None,
        end_date=None,
        search=None,
        use_case=use_case,
    )
    data = resp["data"]["summaries"]
    assert len(data) == 1
    assert data[0]["id"] == s1.id

    # Non-favorites
    resp = await summaries.get_summaries(
        user=user_context,
        limit=20,
        offset=0,
        sort="created_at_desc",
        is_read=None,
        is_favorited=False,
        lang=None,
        start_date=None,
        end_date=None,
        search=None,
        use_case=use_case,
    )
    data = resp["data"]["summaries"]
    ids = [s["id"] for s in data]
    assert s1.id not in ids
    assert s2.id in ids
