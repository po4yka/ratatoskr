"""Ports for proactive signal-source persistence."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SignalSourceRepositoryPort(Protocol):
    """Persistence port for Phase 3 signal-source entities."""

    async def async_upsert_source(
        self,
        *,
        kind: str,
        external_id: str | None = None,
        url: str | None = None,
        title: str | None = None,
        description: str | None = None,
        site_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a generic source."""

    async def async_subscribe(
        self,
        *,
        user_id: int,
        source_id: int,
        topic_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or reactivate a user's source subscription."""

    async def async_get_source(self, source_id: int) -> dict[str, Any] | None:
        """Return a source by ID."""

    async def async_set_source_active(self, source_id: int, *, is_active: bool) -> bool:
        """Enable or disable a source."""

    async def async_set_user_source_active(
        self,
        *,
        user_id: int,
        source_id: int,
        is_active: bool,
    ) -> bool:
        """Enable or disable a source if the user is subscribed to it."""

    async def async_update_user_source_controls(
        self,
        *,
        user_id: int,
        source_id: int,
        is_active: bool | None = None,
        fetch_interval_seconds: int | None = None,
        max_items_per_run: int | None = None,
        retry_policy: dict[str, Any] | None = None,
    ) -> bool:
        """Update operational controls for a source visible to a user."""

    async def async_retry_user_source(self, *, user_id: int, source_id: int) -> bool:
        """Clear backoff for a source visible to a user so the scheduler can retry."""

    async def async_get_source_run_state(self, source_id: int) -> dict[str, Any] | None:
        """Return scheduler state used to decide whether a source can be fetched now."""

    async def async_record_source_fetch_success(self, source_id: int) -> None:
        """Reset source health after a successful fetch."""

    async def async_record_source_fetch_error(
        self,
        *,
        source_id: int,
        error: str,
        max_errors: int,
        base_backoff_seconds: int,
        retry_at: Any | None = None,
    ) -> bool:
        """Record a source fetch failure and return whether the source was disabled."""

    async def async_upsert_feed_item(
        self,
        *,
        source_id: int,
        external_id: str,
        canonical_url: str | None = None,
        title: str | None = None,
        content_text: str | None = None,
        author: str | None = None,
        published_at: Any | None = None,
        engagement: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update an ingested item."""

    async def async_list_user_subscriptions(self, user_id: int) -> list[dict[str, Any]]:
        """List subscriptions visible to a user."""

    async def async_list_source_health(self, *, user_id: int) -> list[dict[str, Any]]:
        """List source health rows visible to a user."""

    async def async_list_user_signals(
        self,
        user_id: int,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List scored signal candidates visible to a user."""

    async def async_upsert_topic(
        self,
        *,
        user_id: int,
        name: str,
        description: str | None = None,
        weight: float = 1.0,
        embedding_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a user's topic preference."""

    async def async_get_user_signal(self, *, user_id: int, signal_id: int) -> dict[str, Any] | None:
        """Return one scored signal candidate visible to a user."""

    async def async_record_user_signal(
        self,
        *,
        user_id: int,
        feed_item_id: int,
        topic_id: int | None = None,
        status: str = "candidate",
        heuristic_score: float | None = None,
        llm_score: float | None = None,
        final_score: float | None = None,
        evidence: dict[str, Any] | None = None,
        filter_stage: str = "heuristic",
        llm_judge: dict[str, Any] | None = None,
        llm_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Persist one scored signal candidate."""

    async def async_update_user_signal_status(
        self,
        *,
        user_id: int,
        signal_id: int,
        status: str,
    ) -> bool:
        """Update one signal status if it belongs to the user."""

    async def async_hide_signal_source(self, *, user_id: int, signal_id: int) -> bool:
        """Disable the source behind one of the user's signals."""

    async def async_boost_signal_topic(
        self,
        *,
        user_id: int,
        signal_id: int,
        increment: float = 0.25,
    ) -> bool:
        """Boost the topic attached to one of the user's signals."""

    async def async_list_unscored_candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """List active subscription/feed-item pairs that do not have a signal yet."""
