from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from app.api.exceptions import ValidationError
from app.api.services._digest_api_categories import DigestCategoryService
from app.config.digest import ChannelDigestConfig
from app.infrastructure.persistence.digest_store import DigestStore


class _FakeDigestStore:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def get_subscription_for_user(self, *, user_id: int, subscription_id: int) -> object | None:
        if user_id == 1001 and subscription_id == 11:
            return SimpleNamespace(id=11, category_id=None)
        return None

    def get_category_for_user(self, user_id: int, category_id: int) -> object | None:
        if user_id == 1001 and category_id == 21:
            return SimpleNamespace(id=21)
        return None

    def save_model(self, model: object) -> None:
        self.saved.append(model)


def test_digest_assign_category_rejects_cross_user_subscription_id() -> None:
    service = DigestCategoryService(ChannelDigestConfig(enabled=True))
    store = _FakeDigestStore()
    service._store = cast("DigestStore", store)

    with pytest.raises(ValidationError, match="Subscription not found"):
        service.assign_category(user_id=1001, subscription_id=99, category_id=21)

    assert store.saved == []


def test_digest_assign_category_rejects_cross_user_category_id() -> None:
    service = DigestCategoryService(ChannelDigestConfig(enabled=True))
    store = _FakeDigestStore()
    service._store = cast("DigestStore", store)

    with pytest.raises(ValidationError, match="Category not found"):
        service.assign_category(user_id=1001, subscription_id=11, category_id=99)

    assert store.saved == []
