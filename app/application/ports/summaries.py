"""Summary and tag ports."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.domain.models.request import RequestStatus

if TYPE_CHECKING:
    from datetime import datetime

    from app.db.session import Database


@runtime_checkable
class SummaryRepositoryPort(Protocol):
    """Port for summary query/update operations used in application use cases."""

    async def async_get_user_summaries(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
        is_read: bool | None = None,
        is_favorited: bool | None = None,
        lang: str | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        sort: str = "created_at_desc",
        search: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Return user summaries with pagination metadata.

        When *search* is provided, restrict to rows whose
        ``Request.title`` matches the term case-insensitively.
        """

    async def async_get_summary_stubs_for_recommendations(
        self,
        user_id: int,
        *,
        is_read: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return lightweight stubs (id + topic_tags) for recommendation scoring.

        Uses the denormalized ``topic_tags`` column; never loads ``json_payload``.
        The ``user_id`` predicate is a defense-in-depth IDOR guard.
        """

    async def async_get_user_summaries_for_insights(
        self,
        user_id: int,
        request_created_after: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return summary rows used for insights/statistics."""

    async def async_get_user_summary_activity_dates(
        self,
        user_id: int,
        created_after: datetime,
    ) -> list[Any]:
        """Return summary activity timestamps for streak calculations."""

    async def async_get_unread_summaries(
        self,
        user_id: int | None,
        chat_id: int | None,
        limit: int = 10,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return unread summaries for user/chat."""

    async def async_get_summary_by_id(self, summary_id: int) -> dict[str, Any] | None:
        """Return summary by ID."""

    async def async_get_summary_context_by_id(self, summary_id: int) -> dict[str, Any] | None:
        """Return summary joined with its request and crawl result."""

    async def async_get_aggregation_source_bundle_for_summary_owned_by_user(
        self, summary_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """Return the latest aggregation source bundle for a summary owned by user_id."""

    async def async_get_summary_by_request(self, request_id: int) -> dict[str, Any] | None:
        """Return summary by request ID."""

    async def async_get_summary_id_by_request(self, request_id: int) -> int | None:
        """Return summary ID by request ID."""

    async def async_get_summaries_by_request_ids(
        self, request_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Return summaries mapped by request ID."""

    async def async_get_all_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """Return all summaries for sync operations."""

    async def async_get_summary_for_sync_apply(
        self, summary_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """Return a summary validated for sync-apply ownership."""

    async def async_apply_sync_change(
        self,
        summary_id: int,
        *,
        is_deleted: bool | None = None,
        deleted_at: datetime | None = None,
        is_read: bool | None = None,
        is_favorited: bool | None = None,
    ) -> int:
        """Apply a sync mutation and return the new server version."""

    async def async_mark_summary_as_read(self, summary_id: int) -> None:
        """Mark summary as read."""

    async def async_bulk_mark_summaries_as_read(
        self, *, user_id: int, summary_ids: list[int]
    ) -> int:
        """Bulk-mark summaries as read; return rows updated.

        Implementations MUST scope the UPDATE to summaries whose
        ``Request.user_id == user_id`` to prevent privilege escalation
        via crafted summary IDs from another user.
        """

    async def async_bulk_set_summaries_favorite(
        self, *, user_id: int, summary_ids: list[int], value: bool
    ) -> int:
        """Bulk set favorite state; return rows updated. User-scoped."""

    async def async_bulk_soft_delete_summaries(
        self, *, user_id: int, summary_ids: list[int]
    ) -> int:
        """Bulk soft-delete summaries; return rows updated. User-scoped."""

    async def async_mark_summary_as_unread(self, summary_id: int) -> None:
        """Mark summary as unread."""

    async def async_get_unread_summary_by_request_id(
        self, request_id: int
    ) -> dict[str, Any] | None:
        """Return unread summary by request ID."""

    async def async_upsert_summary(
        self,
        request_id: int,
        lang: str,
        json_payload: dict[str, Any],
        insights_json: dict[str, Any] | None = None,
        is_read: bool = False,
    ) -> int:
        """Create or update a summary."""

    async def async_finalize_request_summary(
        self,
        request_id: int,
        lang: str,
        json_payload: dict[str, Any],
        insights_json: dict[str, Any] | None = None,
        is_read: bool = False,
        request_status: RequestStatus = RequestStatus.COMPLETED,
    ) -> int:
        """Persist summary and update request status."""

    async def async_update_summary_insights(
        self,
        request_id: int,
        insights_json: dict[str, Any],
    ) -> None:
        """Persist summary insights JSON."""

    async def async_update_reading_progress(
        self,
        summary_id: int,
        progress: float,
        last_read_offset: int,
    ) -> None:
        """Update reading progress and last-read offset."""

    async def async_soft_delete_summary(self, summary_id: int) -> None:
        """Soft-delete summary."""

    async def async_soft_delete_summary_for_user(self, summary_id: int, user_id: int) -> bool:
        """Soft-delete a summary scoped to *user_id* (IDOR guard).

        Returns True if the row was found and deleted, False if it does not
        exist or belongs to a different user.
        """

    async def async_toggle_favorite(self, summary_id: int) -> bool:
        """Toggle favorite status and return the new state."""

    async def async_set_favorite(self, summary_id: int, value: bool) -> None:
        """Persist an explicit favorite state for a summary."""

    async def async_set_favorite_for_user(self, summary_id: int, user_id: int, value: bool) -> bool:
        """Persist an explicit favorite state scoped to *user_id* (IDOR guard).

        Returns True if the row was found and updated, False if it does not
        exist or belongs to a different user.
        """

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version for summaries owned by *user_id*."""

    async def async_upsert_feedback(
        self,
        user_id: int,
        summary_id: int,
        rating: int | None,
        issues: list[str] | None,
        comment: str | None,
    ) -> dict[str, Any]:
        """Create or update feedback for a summary. Returns the feedback record dict."""


type SummaryRepositoryFactory = Callable[[Database], SummaryRepositoryPort]


@runtime_checkable
class TagRepositoryPort(Protocol):
    """Port for tag CRUD and summary-tag association operations."""

    async def async_get_user_tags(self, user_id: int) -> list[dict[str, Any]]:
        """Return all tags owned by a user."""

    async def async_get_tag_by_id(self, tag_id: int) -> dict[str, Any] | None:
        """Return tag by ID."""

    async def async_create_tag(
        self,
        user_id: int,
        name: str,
        normalized_name: str,
        color: str | None,
    ) -> dict[str, Any]:
        """Create a tag and return the created record."""

    async def async_update_tag(
        self,
        tag_id: int,
        name: str | None,
        color: str | None,
    ) -> dict[str, Any]:
        """Update a tag and return the updated record."""

    async def async_delete_tag(self, tag_id: int) -> None:
        """Delete a tag."""

    async def async_attach_tag(
        self,
        summary_id: int,
        tag_id: int,
        source: str,
    ) -> dict[str, Any]:
        """Attach a tag to a summary and return the association record."""

    async def async_detach_tag(self, summary_id: int, tag_id: int) -> None:
        """Detach a tag from a summary."""

    async def async_restore_tag(self, tag_id: int, *, name: str | None = None) -> dict[str, Any]:
        """Restore a previously soft-deleted tag."""

    async def async_get_tags_for_summary(self, summary_id: int) -> list[dict[str, Any]]:
        """Return all tags attached to a summary."""

    async def async_get_tagged_summaries(
        self,
        *,
        user_id: int,
        tag_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return recent summaries for a tag owned by the user."""

    async def async_merge_tags(self, source_tag_ids: list[int], target_tag_id: int) -> None:
        """Merge source tags into target tag, reassigning all associations."""

    async def async_get_tag_by_normalized_name(
        self,
        user_id: int,
        normalized_name: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Return tag by normalized name within a user scope."""
