from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.api.exceptions import AuthorizationError, ResourceNotFoundError
from app.api.services.auth_service import AuthService
from app.core.time_utils import UTC
from app.db.models import User


async def _load_user(db, telegram_user_id: int) -> User | None:
    async with db.session() as session:
        return await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


@pytest.mark.asyncio
async def test_require_owner_allows_owner_and_rejects_non_owner(db, user_factory) -> None:
    owner = await user_factory(username="owner-service", telegram_user_id=1001, is_owner=True)
    regular = await user_factory(username="regular-service", telegram_user_id=1002, is_owner=False)

    owner_record = await AuthService.require_owner({"user_id": owner.telegram_user_id})  # type: ignore[typeddict-item]

    assert owner_record["telegram_user_id"] == owner.telegram_user_id

    with pytest.raises(AuthorizationError):
        await AuthService.require_owner({"user_id": regular.telegram_user_id})  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_get_target_user_creates_new_user_and_ensure_user_fetches_existing(db) -> None:
    target = await AuthService.get_or_create_target_user(2001, username="new-user")

    assert target["telegram_user_id"] == 2001
    created = await _load_user(db, 2001)
    assert created is not None
    assert created.username == "new-user"

    ensured = await AuthService.ensure_user(2001)
    assert ensured["telegram_user_id"] == 2001

    with pytest.raises(ResourceNotFoundError):
        await AuthService.ensure_user(999999)


@pytest.mark.asyncio
async def test_set_and_clear_link_nonce_round_trips_to_database(db, user_factory) -> None:
    user = await user_factory(username="nonce-user", telegram_user_id=3001)
    expires_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    await AuthService.set_link_nonce(user.telegram_user_id, "nonce-123", expires_at)

    refreshed = await _load_user(db, user.telegram_user_id)
    assert refreshed is not None
    assert refreshed.link_nonce == "nonce-123"
    assert refreshed.link_nonce_expires_at == expires_at

    await AuthService.clear_link_nonce(user.telegram_user_id)

    cleared = await _load_user(db, user.telegram_user_id)
    assert cleared is not None
    assert cleared.link_nonce is None
    assert cleared.link_nonce_expires_at is None


def test_build_link_status_payload_and_format_datetime() -> None:
    linked_at = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    expires_at = datetime(2026, 1, 1, 10, 0)
    payload = AuthService.build_link_status_payload(
        {
            "linked_telegram_user_id": 777,
            "linked_telegram_username": "linked_user",
            "linked_telegram_photo_url": "https://example.com/avatar.png",
            "linked_telegram_first_name": "Linked",
            "linked_telegram_last_name": "User",
            "linked_at": linked_at,
            "link_nonce_expires_at": expires_at,
            "link_nonce": "nonce-xyz",
        }
    )

    assert payload.linked is True
    assert payload.telegram_user_id == 777
    assert payload.linked_at == "2026-01-01T09:30:00Z"
    assert payload.link_nonce_expires_at == "2026-01-01T10:00:00Z"
    assert AuthService.format_datetime(linked_at) == "2026-01-01T09:30:00Z"


@pytest.mark.asyncio
async def test_complete_unlink_and_delete_user_update_persisted_state(db, user_factory) -> None:
    user = await user_factory(username="link-user", telegram_user_id=4001)

    await AuthService.complete_telegram_link(
        user_id=user.telegram_user_id,
        telegram_user_id=5555,
        username="linked-account",
        photo_url="https://example.com/photo.png",
        first_name="Linked",
        last_name="Account",
    )

    linked = await _load_user(db, user.telegram_user_id)
    assert linked is not None
    assert linked.linked_telegram_user_id == 5555
    assert linked.linked_telegram_username == "linked-account"

    await AuthService.unlink_telegram(user.telegram_user_id)

    unlinked = await _load_user(db, user.telegram_user_id)
    assert unlinked is not None
    assert unlinked.linked_telegram_user_id is None
    assert unlinked.linked_at is None

    await AuthService.delete_user(user.telegram_user_id)

    assert await _load_user(db, user.telegram_user_id) is None
