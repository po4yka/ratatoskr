import pytest

from app.api.routers.auth.tokens import create_access_token
from app.db.models import CrawlResult, Request, Summary


@pytest.fixture
async def summary_with_content(client, db, user_factory, monkeypatch):
    """Fixture to create a summary with associated CrawlResult content.

    Depends on `client` to ensure database_proxy is properly initialized before model operations.
    """
    user = await user_factory()
    monkeypatch.setenv("ALLOWED_USER_IDS", str(user.telegram_user_id))
    monkeypatch.setenv("REDIS_ENABLED", "0")
    async with db.transaction() as session:
        req = Request(
            user_id=user.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/article",
            normalized_url="https://example.com/article",
        )
        session.add(req)
        await session.flush()
        summary = Summary(
            request_id=req.id,
            lang="en",
            json_payload={"metadata": {"title": "Example Article", "domain": "example.com"}},
        )
        session.add_all(
            [
                summary,
                CrawlResult(
                    request_id=req.id,
                    source_url=req.input_url,
                    content_markdown="# Heading\n\nBody text.",
                    metadata_json={
                        "title": "Example Article",
                        "domain": "example.com",
                    },
                ),
            ]
        )
        await session.flush()
    return user, summary


def test_get_summary_content_markdown(client, summary_with_content):
    user, summary = summary_with_content
    token = create_access_token(user.telegram_user_id, client_id="test")

    response = client.get(
        f"/v1/summaries/{summary.id}/content", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    payload = response.json()
    content = payload["data"]["content"]
    assert content["summaryId"] == summary.id
    assert content["format"] == "markdown"
    assert content["contentType"] == "text/markdown"
    assert "Body text." in content["content"]
    assert content["checksumSha256"]
    assert content["sizeBytes"] > 0


def test_get_summary_content_text_format(client, summary_with_content):
    user, summary = summary_with_content
    token = create_access_token(user.telegram_user_id, client_id="test")

    response = client.get(
        f"/v1/summaries/{summary.id}/content",
        params={"format": "text"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    content = response.json()["data"]["content"]
    assert content["format"] == "text"
    assert content["contentType"] == "text/plain"
    assert "Body text." in content["content"]


async def test_get_summary_content_not_found(client, db, user_factory, monkeypatch):
    user = await user_factory()
    monkeypatch.setenv("ALLOWED_USER_IDS", str(user.telegram_user_id))
    monkeypatch.setenv("REDIS_ENABLED", "0")
    async with db.transaction() as session:
        req = Request(
            user_id=user.telegram_user_id,
            type="url",
            status="completed",
            normalized_url="https://example.com/not-found",
        )
        session.add(req)
        await session.flush()
        summary = Summary(request_id=req.id, lang="en")
        session.add(summary)
        await session.flush()
    token = create_access_token(user.telegram_user_id, client_id="test")

    response = client.get(
        f"/v1/summaries/{summary.id}/content", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 404
