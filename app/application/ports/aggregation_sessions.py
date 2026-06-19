"""Aggregation-session persistence port."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.application.dto.aggregation import AggregationFailure, NormalizedSourceDocument
    from app.domain.models.source import (
        AggregationItemStatus,
        AggregationSessionStatus,
        SourceItem,
    )


@runtime_checkable
class AggregationSessionRepositoryPort(Protocol):
    async def async_create_aggregation_session(
        self,
        user_id: int,
        correlation_id: str,
        total_items: int,
        *,
        allow_partial_success: bool = True,
        bundle_metadata: dict[str, Any] | None = None,
    ) -> int:
        """Create an aggregation session."""

    async def async_get_aggregation_session(self, session_id: int) -> dict[str, Any] | None:
        """Return a stored aggregation session."""

    async def async_get_aggregation_session_by_correlation_id(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        """Return a stored aggregation session by correlation ID."""

    async def async_get_user_aggregation_sessions(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return stored aggregation sessions for one user."""

    async def async_count_user_aggregation_sessions(
        self,
        user_id: int,
        *,
        status: str | None = None,
    ) -> int:
        """Return the total aggregation-session count for one user."""

    async def async_delete_aggregation_session_for_user(
        self,
        session_id: int,
        user_id: int,
    ) -> bool:
        """Delete one aggregation session owned by a user."""

    async def async_add_aggregation_session_item(
        self,
        session_id: int,
        source_item: SourceItem,
        position: int,
        *,
        request_id: int | None = None,
    ) -> int:
        """Persist one source item for an aggregation session."""

    async def async_get_aggregation_session_items(self, session_id: int) -> list[dict[str, Any]]:
        """Return ordered aggregation session items."""

    async def async_update_aggregation_session_item_result(
        self,
        item_id: int,
        *,
        status: AggregationItemStatus | str,
        request_id: int | None = None,
        normalized_document: NormalizedSourceDocument | None = None,
        extraction_metadata: dict[str, Any] | None = None,
        failure: AggregationFailure | None = None,
    ) -> None:
        """Persist the latest extraction outcome for one session item."""

    async def async_update_aggregation_session_counts(
        self,
        session_id: int,
        *,
        successful_count: int,
        failed_count: int,
        duplicate_count: int,
    ) -> None:
        """Persist bundle counters."""

    async def async_update_aggregation_session_output(
        self,
        session_id: int,
        aggregation_output: dict[str, Any],
    ) -> None:
        """Persist synthesized bundle output."""

    async def async_update_aggregation_session_status(
        self,
        session_id: int,
        *,
        status: AggregationSessionStatus | str,
        processing_time_ms: int | None = None,
        failure: AggregationFailure | None = None,
    ) -> None:
        """Persist bundle lifecycle state."""
