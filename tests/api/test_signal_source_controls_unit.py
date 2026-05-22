from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.models.signals import SourceControlRequest
from app.api.routers.social.signals import retry_source, update_source_controls


class _Repo:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.controls: dict | None = None
        self.retry: dict | None = None

    async def async_update_user_source_controls(self, **kwargs):
        self.controls = kwargs
        return self.allow

    async def async_retry_user_source(self, **kwargs):
        self.retry = kwargs
        return self.allow


@pytest.mark.asyncio
async def test_update_source_controls_authorizes_by_user_source_membership() -> None:
    repo = _Repo()

    response = await update_source_controls(
        42,
        SourceControlRequest(
            is_active=False,
            fetch_interval_seconds=900,
            max_items_per_run=25,
            retry_policy={"max_errors": 3},
        ),
        repo=repo,  # type: ignore[arg-type]
        user={"user_id": 1001},
    )

    assert response["data"] == {"updated": True}
    assert repo.controls == {
        "user_id": 1001,
        "source_id": 42,
        "is_active": False,
        "fetch_interval_seconds": 900,
        "max_items_per_run": 25,
        "retry_policy": {"max_errors": 3},
    }


@pytest.mark.asyncio
async def test_retry_source_returns_not_found_for_unowned_source() -> None:
    repo = _Repo(allow=False)

    with pytest.raises(HTTPException) as exc_info:
        await retry_source(42, repo=repo, user={"user_id": 1001})  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404
