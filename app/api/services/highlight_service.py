"""Service logic for summary highlight endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.api.dependencies.database import get_session_manager
from app.api.exceptions import ResourceNotFoundError
from app.api.models.responses import HighlightResponse
from app.api.search_helpers import isotime
from app.db.session import Database  # noqa: TC001  # used at runtime in __init__ signature
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.api.models.requests import CreateHighlightRequest, UpdateHighlightRequest


class SummaryHighlightService:
    """Owns highlight persistence and summary ownership checks."""

    def __init__(self, session_manager: Database | None = None) -> None:
        self._db = session_manager or get_session_manager()
        self._user_content_repo = UserContentRepositoryAdapter(self._db)

    async def list_highlights(self, *, user_id: int, summary_id: int) -> list[dict[str, Any]]:
        """List highlights for an owned summary."""
        await self._verify_summary_ownership(summary_id=summary_id, user_id=user_id)
        highlights = await self._user_content_repo.async_list_highlights(
            user_id=user_id,
            summary_id=summary_id,
        )
        return [self._highlight_to_payload(highlight) for highlight in highlights]

    async def create_highlight(
        self,
        *,
        user_id: int,
        summary_id: int,
        body: CreateHighlightRequest,
    ) -> dict[str, Any]:
        """Create a highlight for an owned summary."""
        await self._verify_summary_ownership(summary_id=summary_id, user_id=user_id)
        highlight = await self._user_content_repo.async_create_highlight(
            user_id=user_id,
            summary_id=summary_id,
            text=body.text,
            start_offset=body.start_offset,
            end_offset=body.end_offset,
            color=body.color,
            note=body.note,
        )
        return self._highlight_to_payload(highlight)

    async def update_highlight(
        self,
        *,
        user_id: int,
        summary_id: int,
        highlight_id: str,
        body: UpdateHighlightRequest,
    ) -> dict[str, Any]:
        """Update a highlight owned by the user."""
        await self._get_owned_highlight(
            user_id=user_id,
            summary_id=summary_id,
            highlight_id=highlight_id,
        )
        highlight = await self._user_content_repo.async_update_highlight(
            highlight_id=highlight_id,
            color=body.color,
            note=body.note,
        )
        return self._highlight_to_payload(highlight)

    async def delete_highlight(
        self,
        *,
        user_id: int,
        summary_id: int,
        highlight_id: str,
    ) -> None:
        """Delete a highlight owned by the user."""
        await self._get_owned_highlight(
            user_id=user_id,
            summary_id=summary_id,
            highlight_id=highlight_id,
        )
        await self._user_content_repo.async_delete_highlight(highlight_id)

    async def _verify_summary_ownership(self, *, summary_id: int, user_id: int) -> None:
        summary = await self._user_content_repo.async_get_owned_summary(
            user_id=user_id, summary_id=summary_id
        )
        if summary is None:
            raise ResourceNotFoundError("Summary", summary_id) from None

    async def _get_owned_highlight(
        self,
        *,
        user_id: int,
        summary_id: int,
        highlight_id: str,
    ) -> Any:
        await self._verify_summary_ownership(summary_id=summary_id, user_id=user_id)
        highlight = await self._user_content_repo.async_get_highlight(
            user_id=user_id,
            summary_id=summary_id,
            highlight_id=highlight_id,
        )
        if highlight is None:
            raise ResourceNotFoundError("Highlight", highlight_id) from None
        return highlight

    @staticmethod
    def _highlight_to_payload(highlight: Any) -> dict[str, Any]:
        return HighlightResponse(
            id=str(highlight.get("id")),
            summary_id=str(highlight.get("summary")),
            text=str(highlight.get("text") or ""),
            start_offset=int(highlight.get("start_offset") or 0),
            end_offset=int(highlight.get("end_offset") or 0),
            color=highlight.get("color"),
            note=highlight.get("note"),
            created_at=isotime(highlight.get("created_at")),
            updated_at=isotime(highlight.get("updated_at")),
        ).model_dump(by_alias=True)
