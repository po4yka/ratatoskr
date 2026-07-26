from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.api.routers.auth.tokens import create_access_token
from app.config import Config
from app.db.models import FeedItem, Source, Subscription, UserSignal

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _headers(user_id: int) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_signal_feed_feedback_and_source_health(client: TestClient, db, user_factory):
    user_id = int(Config.get_allowed_user_ids()[0])
    user = await user_factory(telegram_user_id=user_id, username="signals-user")
    async with db.transaction() as session:
        source = Source(
            kind="rss",
            external_id="https://example.com/feed.xml",
            url="https://example.com/feed.xml",
            title="Example Feed",
            fetch_error_count=1,
            last_error="timeout",
        )
        session.add(source)
        await session.flush()
        item = FeedItem(
            source_id=source.id,
            external_id="guid-1",
            canonical_url="https://example.com/post",
            title="Signal item",
        )
        session.add_all(
            [
                Subscription(
                    user_id=user.telegram_user_id,
                    source_id=source.id,
                    is_active=True,
                ),
                item,
            ]
        )
        await session.flush()
        signal = UserSignal(
            user_id=user.telegram_user_id,
            feed_item_id=item.id,
            status="candidate",
            heuristic_score=0.8,
            final_score=0.8,
        )
        session.add(signal)
        await session.flush()

    headers = _headers(user.telegram_user_id)
    list_response = client.get("/v1/signals", headers=headers)
    health_response = client.get("/v1/signals/sources/health", headers=headers)
    source_active_response = client.post(
        f"/v1/signals/sources/{source.id}/active",
        headers=headers,
        json={"is_active": False},
    )
    feedback_response = client.post(
        f"/v1/signals/{signal.id}/feedback",
        headers=headers,
        json={"action": "like"},
    )

    assert list_response.status_code == 200
    assert list_response.json()["data"]["signals"][0]["feed_item_title"] == "Signal item"
    assert health_response.status_code == 200
    health_rows = health_response.json()["data"]["sources"]
    assert health_rows[0]["title"] == "Example Feed"
    assert health_rows[0]["fetch_error_count"] == 1
    assert health_rows[0]["last_error"] == "timeout"
    assert source_active_response.status_code == 200
    async with db.session() as session:
        refreshed_source = await session.get(Source, source.id)
    assert refreshed_source is not None
    assert refreshed_source.is_active is False
    assert feedback_response.status_code == 200
    async with db.session() as session:
        refreshed_signal = await session.get(UserSignal, signal.id)
    assert refreshed_signal is not None
    assert refreshed_signal.status == "liked"


@pytest.mark.asyncio
async def test_signal_health_reports_vector_readiness(client: TestClient, db, user_factory):
    user_id = int(Config.get_allowed_user_ids()[0])
    user = await user_factory(telegram_user_id=user_id, username="signals-health")
    headers = _headers(user.telegram_user_id)

    response = client.get("/v1/signals/health", headers=headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert "vector" in data
    assert "ready" in data["vector"]
    assert "sources" in data
