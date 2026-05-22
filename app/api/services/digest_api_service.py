"""Service layer for Digest Mini App API."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.api.services._digest_api_categories import DigestCategoryService
from app.api.services._digest_api_channels import DigestChannelService
from app.api.services._digest_api_preferences import DigestPreferenceService
from app.api.services._digest_api_shared import require_enabled, track_background_task
from app.api.services._digest_api_triggers import DigestTriggerService

if TYPE_CHECKING:
    from app.api.models.digest import (
        CategoryResponse,
        DigestPreferenceResponse,
        ResolveChannelResponse,
        TriggerDigestResponse,
    )
    from app.config.digest import ChannelDigestConfig


class DigestAPIService:
    """Stateless service for digest operations via REST API."""

    def __init__(self, digest_config: ChannelDigestConfig) -> None:
        self._cfg = digest_config
        self._channels = DigestChannelService(digest_config)
        self._preferences = DigestPreferenceService(digest_config)
        self._triggers = DigestTriggerService(digest_config)
        self._categories = DigestCategoryService(digest_config)

    def _require_enabled(self) -> None:
        require_enabled(self._cfg)

    def list_subscriptions(self, user_id: int) -> dict[str, Any]:
        return self._channels.list_subscriptions(user_id)

    def subscribe_channel(self, user_id: int, raw_username: str) -> dict[str, str]:
        return self._channels.subscribe_channel(user_id, raw_username)

    def unsubscribe_channel(self, user_id: int, raw_username: str) -> dict[str, str]:
        return self._channels.unsubscribe_channel(user_id, raw_username)

    def update_channel_controls(
        self,
        user_id: int,
        raw_username: str,
        **fields: Any,
    ) -> dict[str, object]:
        return self._channels.update_channel_controls(user_id, raw_username, **fields)

    def retry_channel(self, user_id: int, raw_username: str) -> dict[str, object]:
        return self._channels.retry_channel(user_id, raw_username)

    async def resolve_channel(self, user_id: int, raw_username: str) -> ResolveChannelResponse:
        return await self._channels.resolve_channel(user_id, raw_username)

    def list_channel_posts(
        self,
        user_id: int,
        raw_username: str,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._channels.list_channel_posts(
            user_id,
            raw_username,
            limit=limit,
            offset=offset,
        )

    def get_preferences(self, user_id: int) -> DigestPreferenceResponse:
        return self._preferences.get_preferences(user_id)

    def update_preferences(self, user_id: int, **fields: Any) -> DigestPreferenceResponse:
        return self._preferences.update_preferences(user_id, **fields)

    def list_deliveries(self, user_id: int, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self._preferences.list_deliveries(user_id, limit=limit, offset=offset)

    def trigger_digest(self, user_id: int) -> TriggerDigestResponse:
        return self._triggers.trigger_digest(user_id)

    def trigger_channel_digest(self, user_id: int, raw_channel_username: str) -> dict[str, str]:
        return self._triggers.trigger_channel_digest(user_id, raw_channel_username)

    def enqueue_digest_trigger(self, *, user_id: int, correlation_id: str) -> None:
        task = asyncio.create_task(
            self._execute_digest_trigger(user_id=user_id, correlation_id=correlation_id)
        )
        track_background_task(task)

    def enqueue_channel_digest_trigger(
        self,
        *,
        user_id: int,
        correlation_id: str,
        channel_username: str,
    ) -> None:
        task = asyncio.create_task(
            self._execute_channel_digest_trigger(
                user_id=user_id,
                correlation_id=correlation_id,
                channel_username=channel_username,
            )
        )
        track_background_task(task)

    async def _execute_digest_trigger(self, *, user_id: int, correlation_id: str) -> None:
        await self._triggers.execute_digest_trigger(
            user_id=user_id,
            correlation_id=correlation_id,
            run_digest_task=self._run_digest_task,
        )

    async def _execute_channel_digest_trigger(
        self,
        *,
        user_id: int,
        correlation_id: str,
        channel_username: str,
    ) -> None:
        await self._triggers.execute_channel_digest_trigger(
            user_id=user_id,
            correlation_id=correlation_id,
            channel_username=channel_username,
            run_digest_task=self._run_digest_task,
        )

    async def _run_digest_task(
        self,
        *,
        user_id: int,
        correlation_id: str,
        channel_username: str | None,
    ) -> Any:
        return await self._triggers.run_digest_task(
            user_id=user_id,
            correlation_id=correlation_id,
            channel_username=channel_username,
        )

    def list_categories(self, user_id: int) -> list[CategoryResponse]:
        return self._categories.list_categories(user_id)

    def create_category(self, user_id: int, name: str) -> CategoryResponse:
        return self._categories.create_category(user_id, name)

    def update_category(self, user_id: int, category_id: int, **fields: Any) -> CategoryResponse:
        return self._categories.update_category(user_id, category_id, **fields)

    def delete_category(self, user_id: int, category_id: int) -> dict[str, str]:
        return self._categories.delete_category(user_id, category_id)

    def assign_category(
        self,
        user_id: int,
        subscription_id: int,
        category_id: int | None,
    ) -> dict[str, str]:
        return self._categories.assign_category(user_id, subscription_id, category_id)

    def bulk_unsubscribe(self, user_id: int, usernames: list[str]) -> dict[str, Any]:
        return self._categories.bulk_unsubscribe(user_id, usernames)

    def bulk_assign_category(
        self,
        user_id: int,
        subscription_ids: list[int],
        category_id: int | None,
    ) -> dict[str, Any]:
        return self._categories.bulk_assign_category(user_id, subscription_ids, category_id)
