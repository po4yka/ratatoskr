"""Comprehensive tests for user.py endpoints (preferences + stats + safe_isoformat)."""

from __future__ import annotations

import sys
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

# Mock modules before any app imports
sys.modules["redis"] = MagicMock()
sys.modules["redis.asyncio"] = MagicMock()


class StrEnum(str, Enum):
    """Compatibility shim for StrEnum (Python 3.11+)."""


# The typing.NotRequired shim was REMOVED here (as in tests/conftest.py): it is
# native on Python 3.11+ and globally rebinding it broke langchain-core/langgraph
# pydantic schema generation. The StrEnum shim is retained unchanged.
import enum

enum.StrEnum = StrEnum  # type: ignore[misc,assignment]

from sqlalchemy import select

from app.api.routers.user import get_current_user_profile, get_user_preferences, safe_isoformat
from app.api.routers.user.user import get_user_stats, update_user_preferences
from app.db.models import Request, Summary, User

if TYPE_CHECKING:
    from app.db.session import Database


def _configure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-32-characters-long-123456")
    monkeypatch.setenv("ALLOWED_USER_IDS", "123456789,123456790")
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "com.example.app")


async def _create_user(
    db: Database, *, telegram_user_id: int, username: str, **kwargs: Any
) -> User:
    async with db.transaction() as session:
        existing = await session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        if existing is not None:
            return existing
        user = User(telegram_user_id=telegram_user_id, username=username, **kwargs)
        session.add(user)
        await session.flush()
        return user


async def _create_summary(
    db: Database,
    *,
    user_id: int,
    url: str,
    lang: str = "en",
    is_read: bool = False,
    json_payload: dict | None = None,
) -> int:
    payload = json_payload if isinstance(json_payload, dict) else {}
    raw_reading_time = payload.get("estimated_reading_time_min")
    raw_topic_tags = payload.get("topic_tags")
    async with db.transaction() as session:
        request = Request(
            user_id=user_id,
            input_url=url,
            normalized_url=url,
            status="completed",
            type="url",
        )
        session.add(request)
        await session.flush()
        summary = Summary(
            request_id=request.id,
            lang=lang,
            is_read=is_read,
            json_payload=json_payload,
            reading_time=(int(raw_reading_time) if isinstance(raw_reading_time, int) else None),
            topic_tags=raw_topic_tags if isinstance(raw_topic_tags, list) else None,
        )
        session.add(summary)
        await session.flush()
        return int(summary.id)


# =============================================================================
# Tests for safe_isoformat utility function (pure-Python, no DB)
# =============================================================================


def test_safe_isoformat_with_none() -> None:
    assert safe_isoformat(None) is None


def test_safe_isoformat_with_datetime() -> None:
    dt = datetime(2023, 1, 15, 10, 30, 0)
    result = safe_isoformat(dt)
    assert result == "2023-01-15T10:30:00Z"
    assert result.endswith("Z")


def test_safe_isoformat_with_iso_string() -> None:
    result = safe_isoformat("2023-01-15T10:30:00Z")
    assert result is not None
    assert result.endswith("Z")


def test_safe_isoformat_with_iso_string_plus_timezone() -> None:
    result = safe_isoformat("2023-01-15T10:30:00+00:00")
    assert result is not None
    assert result.endswith("Z")


def test_safe_isoformat_with_invalid_string() -> None:
    result = safe_isoformat("not-a-date")
    assert result == "not-a-date" or result is None


def test_safe_isoformat_with_empty_string() -> None:
    assert safe_isoformat("") is None


def test_safe_isoformat_with_integer() -> None:
    assert safe_isoformat(12345) is None


# =============================================================================
# GET /preferences
# =============================================================================


async def test_get_preferences_default_for_new_user(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    response = await get_user_preferences(user={"user_id": 123456789, "username": "testuser"})
    assert response["success"] is True
    data = response["data"]
    assert data["userId"] == 123456789
    assert data["telegramUsername"] == "testuser"
    assert data["langPreference"] == "en"
    assert data["notificationSettings"]["enabled"] is True
    assert data["notificationSettings"]["frequency"] == "daily"
    assert data["appSettings"]["theme"] == "dark"
    assert data["appSettings"]["font_size"] == "medium"


async def test_get_preferences_with_stored_preferences(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(
        db,
        telegram_user_id=123456789,
        username="testuser",
        preferences_json={
            "lang_preference": "ru",
            "notification_settings": {"enabled": False},
            "custom_field": "value",
        },
    )

    response = await get_user_preferences(user={"user_id": 123456789, "username": "testuser"})
    data = response["data"]
    assert data["langPreference"] == "ru"
    assert data["notificationSettings"]["enabled"] is False
    assert data["appSettings"]["theme"] == "dark"  # merged with defaults


async def test_get_preferences_user_not_found(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)

    response = await get_user_preferences(user={"user_id": 999999, "username": "ghost"})
    assert response["success"] is True
    data = response["data"]
    assert data["userId"] == 999999
    assert data["langPreference"] == "en"


# =============================================================================
# PATCH /preferences
# =============================================================================


async def _read_user(db: Database, telegram_user_id: int) -> User:
    async with db.session() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        assert user is not None
        return user


async def test_get_current_user_profile_uses_typed_defaults(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    response = await get_current_user_profile(user={"user_id": 123456789, "username": "testuser"})

    assert response["success"] is True
    profile = response["data"]["profile"]
    assert profile["userId"] == 123456789
    assert profile["locale"] == "en"
    assert profile["theme"] == "dark"
    assert profile["defaultSummaryLanguage"] == "auto"
    assert profile["onboardingCompletedAt"] is None


async def test_update_current_user_profile_round_trips_typed_fields(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)

    from app.api.models.requests import UpdateUserProfileRequest
    from app.api.routers.user import update_current_user_profile

    response = await update_current_user_profile(
        profile=UpdateUserProfileRequest(
            locale="ru",
            theme="light",
            display_name="Reader",
            default_summary_language="ru",
        ),
        user={"user_id": 123456789, "username": "testuser"},
    )

    profile = response["data"]["profile"]
    assert profile["locale"] == "ru"
    assert profile["theme"] == "light"
    assert profile["displayName"] == "Reader"
    assert profile["defaultSummaryLanguage"] == "ru"

    user = await _read_user(db, 123456789)
    assert user.locale == "ru"
    assert user.theme == "light"
    assert user.display_name == "Reader"
    assert user.default_summary_language == "ru"


async def test_complete_onboarding_sets_timestamp(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)

    from app.api.routers.user import complete_onboarding

    response = await complete_onboarding(user={"user_id": 123456789, "username": "testuser"})

    assert response["data"]["profile"]["onboardingCompletedAt"] is not None
    user = await _read_user(db, 123456789)
    assert user.onboarding_completed_at is not None


async def test_update_preferences_lang_preference(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)

    from app.api.models.requests import UpdatePreferencesRequest

    response = await update_user_preferences(
        preferences=UpdatePreferencesRequest(lang_preference="ru"),
        user={"user_id": 123456789, "username": "testuser"},
    )
    assert response["success"] is True
    assert "lang_preference" in response["data"]["updatedFields"]

    user = await _read_user(db, 123456789)
    assert user.preferences_json["lang_preference"] == "ru"  # type: ignore[index, call-overload]
    assert user.locale == "ru"
    assert user.default_summary_language == "ru"


async def test_update_preferences_notification_settings(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)

    from app.api.models.requests import UpdatePreferencesRequest

    response = await update_user_preferences(
        preferences=UpdatePreferencesRequest(
            notification_settings={"enabled": False, "frequency": "weekly"}
        ),
        user={"user_id": 123456789, "username": "testuser"},
    )
    fields = response["data"]["updatedFields"]
    assert "notification_settings.enabled" in fields
    assert "notification_settings.frequency" in fields

    user = await _read_user(db, 123456789)
    assert user.preferences_json["notification_settings"]["enabled"] is False  # type: ignore[index, call-overload]
    assert user.preferences_json["notification_settings"]["frequency"] == "weekly"  # type: ignore[index, call-overload]


async def test_update_preferences_app_settings(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)

    from app.api.models.requests import UpdatePreferencesRequest

    response = await update_user_preferences(
        preferences=UpdatePreferencesRequest(app_settings={"theme": "light", "font_size": "large"}),
        user={"user_id": 123456789, "username": "testuser"},
    )
    fields = response["data"]["updatedFields"]
    assert "app_settings.theme" in fields
    assert "app_settings.font_size" in fields

    user = await _read_user(db, 123456789)
    assert user.preferences_json["app_settings"]["theme"] == "light"  # type: ignore[index, call-overload]
    assert user.preferences_json["app_settings"]["font_size"] == "large"  # type: ignore[index, call-overload]


async def test_update_preferences_all_fields(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)

    from app.api.models.requests import UpdatePreferencesRequest

    response = await update_user_preferences(
        preferences=UpdatePreferencesRequest(
            lang_preference="en",
            notification_settings={"enabled": True},
            app_settings={"theme": "auto"},
        ),
        user={"user_id": 123456789, "username": "testuser"},
    )
    fields = response["data"]["updatedFields"]
    assert "lang_preference" in fields
    assert "notification_settings.enabled" in fields
    assert "app_settings.theme" in fields


async def test_update_preferences_merge_existing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(
        db,
        telegram_user_id=123456789,
        username="testuser",
        preferences_json={
            "lang_preference": "en",
            "notification_settings": {"enabled": True, "frequency": "daily"},
            "app_settings": {"theme": "dark"},
        },
    )

    from app.api.models.requests import UpdatePreferencesRequest

    await update_user_preferences(
        preferences=UpdatePreferencesRequest(notification_settings={"enabled": False}),
        user={"user_id": 123456789, "username": "testuser"},
    )

    user = await _read_user(db, 123456789)
    assert user.preferences_json["lang_preference"] == "en"  # type: ignore[index, call-overload]
    assert user.preferences_json["notification_settings"]["enabled"] is False  # type: ignore[index, call-overload]
    assert user.preferences_json["notification_settings"]["frequency"] == "daily"  # type: ignore[index, call-overload]
    assert user.preferences_json["app_settings"]["theme"] == "dark"  # type: ignore[index, call-overload]


async def test_update_preferences_empty_request(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)

    from app.api.models.requests import UpdatePreferencesRequest

    response = await update_user_preferences(
        preferences=UpdatePreferencesRequest(),
        user={"user_id": 123456789, "username": "testuser"},
    )
    assert response["data"]["updatedFields"] == []


async def test_update_preferences_app_settings_no_existing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(
        db,
        telegram_user_id=123456789,
        username="testuser",
        preferences_json={
            "lang_preference": "en",
            "notification_settings": {"enabled": True},
        },
    )

    from app.api.models.requests import UpdatePreferencesRequest

    response = await update_user_preferences(
        preferences=UpdatePreferencesRequest(app_settings={"theme": "dark"}),
        user={"user_id": 123456789, "username": "testuser"},
    )
    assert "app_settings.theme" in response["data"]["updatedFields"]

    user = await _read_user(db, 123456789)
    assert user.preferences_json["app_settings"]["theme"] == "dark"  # type: ignore[index, call-overload]


# =============================================================================
# GET /stats
# =============================================================================


async def test_get_stats_no_summaries(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    response = await get_user_stats(user={"user_id": 123456789})
    data = response["data"]
    assert data["totalSummaries"] == 0
    assert data["unreadCount"] == 0
    assert data["readCount"] == 0
    assert data["totalReadingTimeMin"] == 0
    assert data["averageReadingTimeMin"] == 0
    assert data["favoriteTopics"] == []
    assert data["favoriteDomains"] == []
    assert data["languageDistribution"]["en"] == 0
    assert data["languageDistribution"]["ru"] == 0


async def test_get_stats_with_summaries(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    for i in range(3):
        await _create_summary(
            db,
            user_id=123456789,
            url=f"http://test{i}.com/article",
            lang="en",
            is_read=(i == 0),
            json_payload={
                "estimated_reading_time_min": 5,
                "topic_tags": ["tech", "ai"] if i < 2 else ["science"],
                "metadata": {"title": f"Test {i}", "domain": f"test{i}.com"},
            },
        )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert data["totalSummaries"] == 3
    assert data["unreadCount"] == 2
    assert data["readCount"] == 1
    assert data["totalReadingTimeMin"] == 15
    assert data["averageReadingTimeMin"] == 5.0
    assert len(data["favoriteTopics"]) > 0
    assert len(data["favoriteDomains"]) == 3


async def test_get_stats_language_distribution(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    for i, lang in enumerate(["en", "en", "ru"]):
        await _create_summary(
            db,
            user_id=123456789,
            url=f"http://test{i}.com",
            lang=lang,
            json_payload={
                "estimated_reading_time_min": 3,
                "topic_tags": ["tech"],
                "metadata": {"domain": "test.com"},
            },
        )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert data["languageDistribution"]["en"] == 2
    assert data["languageDistribution"]["ru"] == 1


async def test_get_stats_topic_counter(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    for i, tags in enumerate([["tech", "ai"], ["tech", "programming"], ["ai", "ml"]]):
        await _create_summary(
            db,
            user_id=123456789,
            url=f"http://test{i}.com",
            lang="en",
            json_payload={
                "estimated_reading_time_min": 5,
                "topic_tags": tags,
                "metadata": {"domain": "test.com"},
            },
        )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    topics = {t["topic"]: t["count"] for t in data["favoriteTopics"]}
    assert topics["tech"] == 2
    assert topics["ai"] == 2
    assert topics.get("programming") == 1
    assert topics.get("ml") == 1


async def test_get_stats_domain_extraction(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    await _create_summary(
        db,
        user_id=123456789,
        url="http://example.com",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["tech"],
            "metadata": {"domain": "example.com"},
        },
    )
    await _create_summary(
        db,
        user_id=123456789,
        url="http://another.com/article",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["tech"],
            "metadata": {},
        },
    )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    domains = {d["domain"]: d["count"] for d in data["favoriteDomains"]}
    assert "example.com" in domains
    assert len(domains) >= 1


async def test_get_stats_invalid_topic_tags_type(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    await _create_summary(
        db,
        user_id=123456789,
        url="http://test.com",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": "not-a-list",
            "metadata": {"domain": "test.com"},
        },
    )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert data["favoriteTopics"] == []


async def test_get_stats_none_json_payload(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")
    await _create_summary(db, user_id=123456789, url="http://test.com", json_payload=None)

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert data["totalSummaries"] == 1
    assert data["totalReadingTimeMin"] == 0


async def test_get_stats_url_parse_error(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    await _create_summary(
        db,
        user_id=123456789,
        url="invalid-url",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["tech"],
            "metadata": {},
        },
    )

    response = await get_user_stats(user={"user_id": 123456789})
    assert response["success"] is True


async def test_get_stats_last_summary_timestamp(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")
    await _create_summary(
        db,
        user_id=123456789,
        url="http://test.com",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["tech"],
            "metadata": {"domain": "test.com"},
        },
    )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert "lastSummaryAt" in data
    if data["lastSummaryAt"]:
        assert isinstance(data["lastSummaryAt"], str)


async def test_get_stats_joined_at_timestamp(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert "joinedAt" in data
    if data["joinedAt"]:
        assert isinstance(data["joinedAt"], str)


async def test_get_stats_topic_tags_with_none_values(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    await _create_summary(
        db,
        user_id=123456789,
        url="http://test.com",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["valid", None, "", 123, "another"],
            "metadata": {"domain": "test.com"},
        },
    )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    topics = {t["topic"] for t in data["favoriteTopics"]}
    assert "valid" in topics
    assert "another" in topics
    assert None not in topics


async def test_get_stats_language_other_than_en_ru(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    await _create_summary(
        db,
        user_id=123456789,
        url="http://test.com",
        lang="fr",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["tech"],
            "metadata": {"domain": "test.com"},
        },
    )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert data["languageDistribution"]["en"] == 0
    assert data["languageDistribution"]["ru"] == 0
    assert data["totalSummaries"] == 1


async def test_get_stats_domain_extraction_from_request(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_env(monkeypatch)
    await _create_user(db, telegram_user_id=123456789, username="testuser")

    await _create_summary(
        db,
        user_id=123456789,
        url="http://example.org/page",
        json_payload={
            "estimated_reading_time_min": 5,
            "topic_tags": ["tech"],
            "metadata": {},
        },
    )

    data = (await get_user_stats(user={"user_id": 123456789}))["data"]
    assert "favoriteDomains" in data
    assert isinstance(data["favoriteDomains"], list)
