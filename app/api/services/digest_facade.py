"""Facade for digest API orchestration used by HTTP routers.

Rationale: trigger_digest() and trigger_channel_digest() must atomically
compose service.trigger_*() with service.enqueue_*() -- two operations that
callers should not need to orchestrate themselves. The facade provides this
combined step while also isolating routers from config-factory construction.
The pass-through methods (list_channels, subscribe, etc.) exist to give
routers a uniform interface without branching between the two sub-services.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from app.api.services.digest_api_service import DigestAPIService
from app.config.digest import ChannelDigestConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.api.models.digest import (
        CategoryResponse,
        DigestPreferenceResponse,
        ResolveChannelResponse,
        TriggerDigestResponse,
    )


class DigestFacade:
    """Coordinates digest API service composition and async trigger workflows."""

    def __init__(
        self,
        config_factory: Callable[[], ChannelDigestConfig] | DigestAPIService | None = None,
        service_factory: Callable[[ChannelDigestConfig], DigestAPIService] | None = None,
    ) -> None:
        self._svc: DigestAPIService
        if config_factory is not None and hasattr(config_factory, "list_subscriptions"):
            self._svc = cast("DigestAPIService", config_factory)
            return

        cfg_factory = config_factory or ChannelDigestConfig
        svc_factory = service_factory or DigestAPIService
        self._svc = svc_factory(cfg_factory())

    def _service(self) -> DigestAPIService:
        return self._svc

    # --- Channels ---

    def list_channels(self, user_id: int) -> dict[str, Any]:
        return self._service().list_subscriptions(user_id)

    def subscribe_channel(self, user_id: int, channel_username: str) -> dict[str, str]:
        return self._service().subscribe_channel(user_id, channel_username)

    def unsubscribe_channel(self, user_id: int, channel_username: str) -> dict[str, str]:
        return self._service().unsubscribe_channel(user_id, channel_username)

    def update_channel_controls(
        self,
        user_id: int,
        channel_username: str,
        **fields: Any,
    ) -> dict[str, object]:
        return self._service().update_channel_controls(user_id, channel_username, **fields)

    def retry_channel(self, user_id: int, channel_username: str) -> dict[str, object]:
        return self._service().retry_channel(user_id, channel_username)

    async def resolve_channel(self, user_id: int, username: str) -> ResolveChannelResponse:
        return await self._service().resolve_channel(user_id, username)

    # --- Posts ---

    def list_channel_posts(
        self, user_id: int, username: str, *, limit: int, offset: int
    ) -> dict[str, Any]:
        return self._service().list_channel_posts(user_id, username, limit=limit, offset=offset)

    # --- Preferences ---

    def get_preferences(self, user_id: int) -> DigestPreferenceResponse:
        return self._service().get_preferences(user_id)

    def update_preferences(self, user_id: int, **fields: Any) -> DigestPreferenceResponse:
        return self._service().update_preferences(user_id, **fields)

    # --- History ---

    def list_history(self, user_id: int, *, limit: int, offset: int) -> dict[str, Any]:
        return self._service().list_deliveries(user_id, limit=limit, offset=offset)

    # --- Triggers ---

    def trigger_digest(self, user_id: int) -> TriggerDigestResponse:
        service = self._service()
        data = service.trigger_digest(user_id)
        service.enqueue_digest_trigger(
            user_id=user_id,
            correlation_id=data.correlation_id,
        )
        return data

    def trigger_channel_digest(self, user_id: int, channel_username: str) -> dict[str, str]:
        service = self._service()
        data = service.trigger_channel_digest(user_id, channel_username)
        service.enqueue_channel_digest_trigger(
            user_id=user_id,
            channel_username=data["channel"],
            correlation_id=data["correlation_id"],
        )
        return data

    # --- Categories ---

    def list_categories(self, user_id: int) -> list[CategoryResponse]:
        return self._service().list_categories(user_id)

    def create_category(self, user_id: int, name: str) -> CategoryResponse:
        return self._service().create_category(user_id, name)

    def update_category(self, user_id: int, category_id: int, **fields: Any) -> CategoryResponse:
        return self._service().update_category(user_id, category_id, **fields)

    def delete_category(self, user_id: int, category_id: int) -> dict[str, str]:
        return self._service().delete_category(user_id, category_id)

    def assign_category(
        self, user_id: int, subscription_id: int, category_id: int | None
    ) -> dict[str, str]:
        return self._service().assign_category(user_id, subscription_id, category_id)

    # --- Bulk operations ---

    def bulk_unsubscribe(self, user_id: int, usernames: list[str]) -> dict[str, Any]:
        return self._service().bulk_unsubscribe(user_id, usernames)

    def bulk_assign_category(
        self, user_id: int, subscription_ids: list[int], category_id: int | None
    ) -> dict[str, Any]:
        return self._service().bulk_assign_category(user_id, subscription_ids, category_id)


@lru_cache(maxsize=1)
def get_digest_facade() -> DigestFacade:
    """FastAPI dependency provider for DigestFacade."""
    return DigestFacade()
