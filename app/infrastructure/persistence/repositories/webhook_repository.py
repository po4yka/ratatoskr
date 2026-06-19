"""SQLAlchemy implementation of the webhook repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from app.db.json_utils import prepare_json_payload
from app.db.models import WebhookDelivery, WebhookSubscription, model_to_dict
from app.db.types import _utcnow

if TYPE_CHECKING:
    from app.db.session import Database


class WebhookRepositoryAdapter:
    """Adapter for webhook subscription and delivery operations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_user_subscriptions(
        self, user_id: int, enabled_only: bool = True
    ) -> list[dict[str, Any]]:
        """Return webhook subscriptions for a user."""
        async with self._database.session() as session:
            stmt = select(WebhookSubscription).where(
                WebhookSubscription.user_id == user_id,
                WebhookSubscription.is_deleted.is_(False),
            )
            if enabled_only:
                stmt = stmt.where(WebhookSubscription.enabled.is_(True))
            rows = (
                await session.execute(stmt.order_by(WebhookSubscription.created_at.desc()))
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_get_subscription_by_id(self, subscription_id: int) -> dict[str, Any] | None:
        """Return a single subscription by ID."""
        async with self._database.session() as session:
            sub = await session.get(WebhookSubscription, subscription_id)
            return model_to_dict(sub)

    async def async_create_subscription(
        self,
        user_id: int,
        name: str | None,
        url: str,
        secret: str,
        events: list[str],
    ) -> dict[str, Any]:
        """Create a new webhook subscription."""
        async with self._database.transaction() as session:
            sub = WebhookSubscription(
                user_id=user_id,
                name=name,
                url=url,
                secret=secret,
                events_json=prepare_json_payload(events, default=[]),
                enabled=True,
                status="active",
            )
            session.add(sub)
            await session.flush()
            return model_to_dict(sub) or {}

    async def async_update_subscription(
        self, subscription_id: int, **kwargs: Any
    ) -> dict[str, Any]:
        """Update an existing webhook subscription."""
        update_data = dict(kwargs)
        if "events" in update_data:
            update_data["events_json"] = prepare_json_payload(update_data.pop("events"), default=[])
        allowed_fields = set(WebhookSubscription.__mapper__.columns.keys()) - {"id"}
        update_values = {key: value for key, value in update_data.items() if key in allowed_fields}
        update_values["updated_at"] = _utcnow()

        async with self._database.transaction() as session:
            await session.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == subscription_id)
                .values(**update_values)
            )
            sub = await session.get(WebhookSubscription, subscription_id)
            return model_to_dict(sub) or {}

    async def async_delete_subscription(self, subscription_id: int) -> None:
        """Soft-delete a webhook subscription."""
        async with self._database.transaction() as session:
            await session.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == subscription_id)
                .values(
                    is_deleted=True,
                    deleted_at=_utcnow(),
                    enabled=False,
                    updated_at=_utcnow(),
                )
            )

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
        async with self._database.transaction() as session:
            delivery = WebhookDelivery(
                subscription_id=subscription_id,
                event_type=event_type,
                payload_json=prepare_json_payload(payload, default={}),
                response_status=response_status,
                response_body=response_body,
                duration_ms=duration_ms,
                success=success,
                attempt=attempt,
                error=error,
            )
            session.add(delivery)
            await session.flush()
            await session.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == subscription_id)
                .values(last_delivery_at=_utcnow(), updated_at=_utcnow())
            )
            return model_to_dict(delivery) or {}

    async def async_get_deliveries(
        self, subscription_id: int, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return delivery log entries for a subscription."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(WebhookDelivery)
                    .where(WebhookDelivery.subscription_id == subscription_id)
                    .order_by(WebhookDelivery.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_increment_failure_count(self, subscription_id: int) -> int:
        """Increment consecutive failure count atomically. Returns the new count."""
        async with self._database.transaction() as session:
            new_count = await session.scalar(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == subscription_id)
                .values(
                    failure_count=WebhookSubscription.failure_count + 1,
                    updated_at=_utcnow(),
                )
                .returning(WebhookSubscription.failure_count)
            )
            return int(new_count or 0)

    async def async_reset_failure_count(self, subscription_id: int) -> None:
        """Reset consecutive failure count to zero."""
        async with self._database.transaction() as session:
            await session.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == subscription_id)
                .values(failure_count=0, updated_at=_utcnow())
            )

    async def async_disable_subscription(self, subscription_id: int) -> None:
        """Disable a webhook subscription."""
        async with self._database.transaction() as session:
            await session.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == subscription_id)
                .values(status="disabled", enabled=False, updated_at=_utcnow())
            )

    async def async_rotate_secret(self, subscription_id: int, new_secret: str) -> None:
        """Rotate the HMAC secret for a subscription."""
        async with self._database.transaction() as session:
            await session.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.id == subscription_id)
                .values(secret=new_secret, updated_at=_utcnow())
            )
