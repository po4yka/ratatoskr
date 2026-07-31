"""Webhook and automation-rule ports.

Rules are stored, listed and edited; nothing executes them. The ports the
executor needed -- context building, webhook dispatch, rate limiting, collection
membership -- went with it. What remains is the CRUD surface behind /v1/rules and
the Telegram /rules command.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WebhookRepositoryPort(Protocol):
    """Port for webhook subscription and delivery operations."""

    async def async_get_user_subscriptions(
        self, user_id: int, enabled_only: bool = True
    ) -> list[dict[str, Any]]:
        """Return webhook subscriptions for a user."""

    async def async_get_subscription_by_id(self, subscription_id: int) -> dict[str, Any] | None:
        """Return a single subscription by ID."""

    async def async_create_subscription(
        self,
        user_id: int,
        name: str | None,
        url: str,
        secret: str,
        events: list[str],
    ) -> dict[str, Any]:
        """Create a new webhook subscription."""

    async def async_update_subscription(
        self, subscription_id: int, user_id: int, **kwargs: Any
    ) -> dict[str, Any]:
        """Update an existing webhook subscription owned by user_id."""

    async def async_delete_subscription(self, subscription_id: int, user_id: int) -> None:
        """Soft-delete a webhook subscription owned by user_id."""

    async def async_log_delivery(
        self,
        subscription_id: int,
        event_type: str,
        payload: dict[str, Any],
        response_status: int | None,
        response_body: str | None,
        duration_ms: int | None,
        success: bool,
        attempt: int,
        error: str | None,
    ) -> dict[str, Any]:
        """Persist a webhook delivery attempt."""

    async def async_get_deliveries(
        self, subscription_id: int, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return delivery log entries for a subscription."""

    async def async_increment_failure_count(self, subscription_id: int) -> int:
        """Increment consecutive failure count. Returns the new count."""

    async def async_reset_failure_count(self, subscription_id: int) -> None:
        """Reset consecutive failure count to zero."""

    async def async_disable_subscription(self, subscription_id: int) -> None:
        """Disable a webhook subscription."""

    async def async_rotate_secret(
        self, subscription_id: int, new_secret: str, user_id: int
    ) -> None:
        """Rotate the HMAC secret for a subscription owned by user_id."""


@runtime_checkable
class RuleRepositoryPort(Protocol):
    """Port for automation rule CRUD and execution log operations."""

    async def async_get_user_rules(
        self, user_id: int, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return all non-deleted rules owned by a user."""

    async def async_get_rule_by_id(self, rule_id: int) -> dict[str, Any] | None:
        """Return rule by ID."""

    async def async_get_rules_by_event_type(
        self, user_id: int, event_type: str
    ) -> list[dict[str, Any]]:
        """Return enabled rules matching event type, ordered by priority."""

    async def async_create_rule(
        self,
        user_id: int,
        name: str,
        event_type: str,
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        match_mode: str = "all",
        priority: int = 0,
        description: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create a rule and return the created record."""

    async def async_update_rule(self, rule_id: int, user_id: int, **fields: Any) -> dict[str, Any]:
        """Update provided fields on a rule owned by user_id and return the updated record."""

    async def async_soft_delete_rule(self, rule_id: int, user_id: int) -> None:
        """Soft-delete a rule owned by user_id."""

    async def async_increment_run_count(self, rule_id: int) -> None:
        """Increment run_count and set last_triggered_at to now."""

    async def async_create_execution_log(
        self,
        rule_id: int,
        summary_id: int | None,
        event_type: str,
        matched: bool,
        conditions_result: list[dict[str, Any]] | None = None,
        actions_taken: list[dict[str, Any]] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Insert an execution log entry and return the created record."""

    async def async_get_execution_logs(
        self, rule_id: int, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return paginated execution logs for a rule."""
