from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.exceptions import ValidationError
from app.api.models.requests import CreateCustomDigestRequest
from app.api.routers import custom_digests as custom_digest_router
from app.api.services.custom_digest_service import CustomDigestService
from app.core.time_utils import UTC


def _summary(summary_id: int, *, title: str, idea: str, preview: str) -> dict[str, object]:
    return {
        "id": summary_id,
        "lang": "en",
        "request": {"id": summary_id + 100, "input_url": f"https://example.com/{summary_id}"},
        "json_payload": {
            "metadata": {"title": title},
            "key_ideas": [idea],
            "summary_250": preview,
        },
    }


def _service(repo: SimpleNamespace) -> CustomDigestService:
    return CustomDigestService(
        session_manager=SimpleNamespace(),
        user_content_repo=repo,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_synthesized_digest_uses_cited_structured_content() -> None:
    summaries = [
        _summary(11, title="First", idea="The first finding.", preview="First preview."),
        _summary(22, title="Second", idea="The second finding.", preview="Second preview."),
    ]
    repo = SimpleNamespace(
        async_get_owned_summaries=AsyncMock(return_value=summaries),
        async_create_custom_digest=AsyncMock(
            return_value={
                "id": uuid4(),
                "title": "Synthesis",
                "content": "stored",
                "status": "ready",
                "created_at": datetime(2026, 7, 11, tzinfo=UTC),
            }
        ),
    )

    await _service(repo).create_digest(
        user_id=7,
        body=CreateCustomDigestRequest(
            summary_ids=["11", "22"],
            title="Synthesis",
            mode="synthesized",
        ),
    )

    created = repo.async_create_custom_digest.await_args.kwargs
    assert "## Key claims" in created["content"]
    assert "[summary:11]" in created["content"]
    assert "## Disagreements" in created["content"]
    assert "## Complementary perspectives" in created["content"]
    assert "## Suggested reading order" in created["content"]


@pytest.mark.asyncio
async def test_default_digest_mode_preserves_concatenated_summary_previews() -> None:
    summaries = [
        _summary(11, title="First", idea="The first finding.", preview="First preview."),
        _summary(22, title="Second", idea="The second finding.", preview="Second preview."),
    ]
    repo = SimpleNamespace(
        async_get_owned_summaries=AsyncMock(return_value=summaries),
        async_create_custom_digest=AsyncMock(
            return_value={
                "id": uuid4(),
                "title": "Legacy",
                "content": "stored",
                "status": "ready",
                "created_at": datetime(2026, 7, 11, tzinfo=UTC),
            }
        ),
    )

    await _service(repo).create_digest(
        user_id=7,
        body=CreateCustomDigestRequest(summary_ids=["11", "22"], title="Legacy"),
    )

    assert repo.async_create_custom_digest.await_args.kwargs["content"] == (
        "## First\n\nFirst preview.\n\n---\n\n## Second\n\nSecond preview."
    )


@pytest.mark.asyncio
async def test_custom_digest_endpoint_passes_synthesized_mode_and_correlation_id() -> None:
    service = SimpleNamespace(
        create_digest=AsyncMock(return_value={"id": "digest-1", "status": "ready"})
    )
    request = SimpleNamespace(state=SimpleNamespace(correlation_id="digest-correlation"))
    body = CreateCustomDigestRequest(summary_ids=["11"], mode="synthesized")

    response = await custom_digest_router.create_custom_digest(
        body=body,
        request=request,
        user={"user_id": 7},
        service=service,
    )

    service.create_digest.assert_awaited_once_with(
        user_id=7,
        body=body,
        correlation_id="digest-correlation",
    )
    assert response["meta"]["correlation_id"] == "digest-correlation"


@pytest.mark.asyncio
async def test_synthesized_digest_rejects_summaries_without_usable_content() -> None:
    repo = SimpleNamespace(
        async_get_owned_summaries=AsyncMock(
            return_value=[{"id": 11, "lang": "en", "json_payload": {}}]
        ),
        async_create_custom_digest=AsyncMock(),
    )

    with pytest.raises(ValidationError, match="do not contain usable digest content"):
        await _service(repo).create_digest(
            user_id=7,
            body=CreateCustomDigestRequest(summary_ids=["11"], mode="synthesized"),
        )

    repo.async_create_custom_digest.assert_not_awaited()
