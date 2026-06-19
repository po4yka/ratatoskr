"""Preference and delivery history helpers for DigestAPIService."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, TypeVar

from app.api.exceptions import ValidationError
from app.api.models.digest import DigestDeliveryResponse, DigestPreferenceResponse
from app.api.services._digest_api_shared import require_enabled
from app.infrastructure.persistence.digest_store import DigestStore
from app.infrastructure.persistence.email_delivery_store import EmailDeliveryStore

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from app.config.digest import ChannelDigestConfig

T = TypeVar("T")


class DigestPreferenceService:
    """Preference and history operations for digest API callers."""

    def __init__(self, cfg: ChannelDigestConfig) -> None:
        self._cfg = cfg
        self._store = DigestStore()
        self._email_store = EmailDeliveryStore()

    def get_preferences(self, user_id: int) -> DigestPreferenceResponse:
        require_enabled(self._cfg)
        preference = self._store.get_user_preference(user_id)

        def _value(user_value: Any, global_value: Any) -> tuple[Any, str]:
            if user_value is not None:
                return user_value, "user"
            return global_value, "global"

        delivery_time, delivery_time_source = _value(
            preference.delivery_time if preference else None,
            ",".join(self._cfg.digest_times),
        )
        timezone, timezone_source = _value(
            preference.timezone if preference else None,
            self._cfg.timezone,
        )
        hours_lookback, hours_lookback_source = _value(
            preference.hours_lookback if preference else None,
            self._cfg.hours_lookback,
        )
        max_posts, max_posts_source = _value(
            preference.max_posts_per_digest if preference else None,
            self._cfg.max_posts_per_digest,
        )
        min_relevance, min_relevance_source = _value(
            preference.min_relevance_score if preference else None,
            self._cfg.min_relevance_score,
        )
        delivery_channel, delivery_channel_source = _value(
            preference.delivery_channel if preference else None,
            "telegram",
        )
        email_address_id, email_address_id_source = _value(
            preference.email_address_id if preference else None,
            None,
        )

        return DigestPreferenceResponse(
            delivery_time=delivery_time,
            delivery_time_source=delivery_time_source,
            timezone=timezone,
            timezone_source=timezone_source,
            hours_lookback=hours_lookback,
            hours_lookback_source=hours_lookback_source,
            max_posts_per_digest=max_posts,
            max_posts_per_digest_source=max_posts_source,
            min_relevance_score=min_relevance,
            min_relevance_score_source=min_relevance_source,
            delivery_channel=delivery_channel,
            delivery_channel_source=delivery_channel_source,
            email_address_id=email_address_id,
            email_address_id_source=email_address_id_source,
        )

    def update_preferences(self, user_id: int, **fields: Any) -> DigestPreferenceResponse:
        require_enabled(self._cfg)
        delivery_time = fields.get("delivery_time")
        if delivery_time is not None:
            parts = delivery_time.split(":")
            if len(parts) != 2:
                raise ValidationError("delivery_time must be in HH:MM format")
            try:
                hour, minute = int(parts[0]), int(parts[1])
            except ValueError as exc:
                raise ValidationError("delivery_time must contain valid integers") from exc
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValidationError("Invalid hour/minute in delivery_time")
        delivery_channel = fields.get("delivery_channel")
        email_address_id = fields.get("email_address_id")
        if delivery_channel == "email":
            if email_address_id is None:
                raise ValidationError("email_address_id is required when delivery_channel is email")
            verified = _run_async_email_lookup(
                self._email_store.async_get_verified_address_for_user(
                    user_id=user_id,
                    address_id=email_address_id,
                )
            )
            if verified is None:
                raise ValidationError("Email address must be verified before use")

        preference, created = self._store.get_or_create_user_preference(
            user_id,
            {
                "delivery_time": fields.get("delivery_time"),
                "timezone": fields.get("timezone"),
                "hours_lookback": fields.get("hours_lookback"),
                "max_posts_per_digest": fields.get("max_posts_per_digest"),
                "min_relevance_score": fields.get("min_relevance_score"),
                "delivery_channel": fields.get("delivery_channel") or "telegram",
                "email_address_id": fields.get("email_address_id"),
            },
        )
        if not created:
            changed = False
            for key in (
                "delivery_time",
                "timezone",
                "hours_lookback",
                "max_posts_per_digest",
                "min_relevance_score",
                "delivery_channel",
                "email_address_id",
            ):
                value = fields.get(key)
                if value is not None and getattr(preference, key) != value:
                    setattr(preference, key, value)
                    changed = True
            if changed:
                self._store.touch_preference(preference)

        return self.get_preferences(user_id)

    def list_deliveries(self, user_id: int, limit: int = 20, offset: int = 0) -> dict[str, object]:
        require_enabled(self._cfg)
        total = self._store.count_deliveries(user_id)
        deliveries = self._store.list_deliveries(user_id=user_id, limit=limit, offset=offset)
        items = [
            DigestDeliveryResponse(
                id=delivery.id,
                delivered_at=delivery.delivered_at,
                post_count=delivery.post_count,
                channel_count=delivery.channel_count,
                digest_type=delivery.digest_type,
            )
            for delivery in deliveries
        ]
        return {
            "deliveries": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }


def _run_async_email_lookup(coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    msg = "DigestPreferenceService sync methods cannot run inside an active event loop"
    raise RuntimeError(msg)
