"""User preferences, statistics, goals, and streaks endpoints."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies.database import get_summary_repository, get_user_repository
from app.api.models.requests import (
    CreateGoalRequest,
    UpdatePreferencesRequest,
    UpdateUserProfileRequest,
)
from app.api.models.responses import (
    DomainStat,
    PreferencesData,
    PreferencesUpdateResult,
    StreakResponse,
    TopicStat,
    UserMeResponse,
    UserProfileResponse,
    UserStatsData,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.api.services.user_activity_service import UserActivityService
from app.api.services.user_goal_service import UserGoalService
from app.application.services.topic_search_utils import ensure_mapping
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC

logger = get_logger(__name__)
router = APIRouter()
profile_router = APIRouter()


def safe_isoformat(dt_value: Any) -> str | None:
    """Safely convert datetime-ish values to ISO 8601 Z form.

    Handles:
    - datetime objects -> ISO string with Z suffix
    - ISO strings -> normalized with Z suffix
    - None/invalid -> None
    """
    if dt_value is None:
        return None
    if hasattr(dt_value, "isoformat") and not isinstance(dt_value, str):
        return str(dt_value.isoformat()) + "Z"
    if isinstance(dt_value, str):
        try:
            parsed = datetime.fromisoformat(dt_value.replace("Z", "+00:00"))
            return parsed.isoformat() + "Z"
        except (ValueError, AttributeError):
            return dt_value if dt_value else None
    return None


def _profile_from_user_record(
    *,
    user_id: int,
    telegram_username: str | None,
    user_record: dict[str, Any] | None,
) -> UserProfileResponse:
    prefs = ensure_mapping(user_record.get("preferences_json") if user_record else None)
    app_settings = ensure_mapping(prefs.get("app_settings"))
    lang_preference = prefs.get("lang_preference")
    return UserProfileResponse(
        user_id=user_id,
        telegram_username=telegram_username,
        display_name=_profile_str(user_record, "display_name"),
        locale=_profile_str(user_record, "locale") or _valid_lang(lang_preference, default="en"),
        theme=_profile_str(user_record, "theme") or _profile_theme(app_settings.get("theme")),
        default_summary_language=_profile_str(user_record, "default_summary_language")
        or _valid_lang(lang_preference, default="auto"),
        onboarding_completed_at=safe_isoformat(
            user_record.get("onboarding_completed_at") if user_record else None
        ),
        created_at=safe_isoformat(user_record.get("created_at") if user_record else None),
        updated_at=safe_isoformat(user_record.get("updated_at") if user_record else None),
    )


def _profile_str(user_record: dict[str, Any] | None, key: str) -> str | None:
    value = user_record.get(key) if user_record else None
    return value if isinstance(value, str) and value else None


def _valid_lang(value: Any, *, default: str) -> str:
    return value if value in {"auto", "en", "ru"} else default


def _profile_theme(value: Any) -> str:
    return value if value in {"dark", "light", "system"} else "dark"


def _profile_updates_from_preferences(preferences: UpdatePreferencesRequest) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if preferences.lang_preference in {"auto", "en", "ru"}:
        updates["default_summary_language"] = preferences.lang_preference
        if preferences.lang_preference != "auto":
            updates["locale"] = preferences.lang_preference
    app_settings = preferences.app_settings or {}
    theme = app_settings.get("theme")
    if theme in {"dark", "light", "system"}:
        updates["theme"] = theme
    return updates


async def _get_or_create_current_user_record(user: dict[str, Any]) -> dict[str, Any]:
    user_repo = get_user_repository()
    user_record, _created = await user_repo.async_get_or_create_user(
        user["user_id"],
        username=user.get("username"),
        is_owner=False,
    )
    return user_record


@profile_router.get("/me")
@router.get("/me")
async def get_current_user_profile(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Get typed current-user profile."""
    user_record = await _get_or_create_current_user_record(user)
    return success_response(
        UserMeResponse(
            profile=_profile_from_user_record(
                user_id=user["user_id"],
                telegram_username=user.get("username"),
                user_record=user_record,
            )
        )
    )


@profile_router.put("/me")
@router.put("/me")
async def update_current_user_profile(
    profile: UpdateUserProfileRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Update typed current-user profile."""
    user_repo = get_user_repository()
    await _get_or_create_current_user_record(user)
    updates = profile.model_dump(exclude_none=True)
    await user_repo.async_update_user_profile(user["user_id"], **updates)
    user_record = await user_repo.async_get_user_by_telegram_id(user["user_id"])
    return success_response(
        UserMeResponse(
            profile=_profile_from_user_record(
                user_id=user["user_id"],
                telegram_username=user.get("username"),
                user_record=user_record,
            )
        )
    )


@profile_router.post("/me/onboarding/complete")
@router.post("/me/onboarding/complete")
async def complete_onboarding(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Mark first-time onboarding as complete."""
    user_repo = get_user_repository()
    await _get_or_create_current_user_record(user)
    await user_repo.async_update_user_profile(
        user["user_id"],
        onboarding_completed_at=datetime.now(UTC),
    )
    user_record = await user_repo.async_get_user_by_telegram_id(user["user_id"])
    return success_response(
        UserMeResponse(
            profile=_profile_from_user_record(
                user_id=user["user_id"],
                telegram_username=user.get("username"),
                user_record=user_record,
            )
        )
    )


@router.get("/preferences")
async def get_user_preferences(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Get user preferences."""
    user_repo = get_user_repository()
    user_record = await user_repo.async_get_user_by_telegram_id(user["user_id"])

    # Default preferences
    default_preferences: dict[str, Any] = {
        "lang_preference": "en",
        "notification_settings": {"enabled": True, "frequency": "daily"},
        "app_settings": {"theme": "dark", "font_size": "medium"},
    }

    # Build a normalized preference object with safe defaults.
    preferences: dict[str, Any] = {
        "lang_preference": default_preferences["lang_preference"],
        "notification_settings": dict(default_preferences["notification_settings"]),
        "app_settings": dict(default_preferences["app_settings"]),
    }
    stored_preferences = user_record.get("preferences_json") if user_record else None
    if isinstance(stored_preferences, dict):
        lang_preference = stored_preferences.get("lang_preference")
        if isinstance(lang_preference, str) and lang_preference:
            preferences["lang_preference"] = lang_preference

        notification_settings = stored_preferences.get("notification_settings")
        if isinstance(notification_settings, dict):
            preferences["notification_settings"].update(notification_settings)

        app_settings = stored_preferences.get("app_settings")
        if isinstance(app_settings, dict):
            preferences["app_settings"].update(app_settings)

    return success_response(
        PreferencesData(
            user_id=user["user_id"],
            telegram_username=user.get("username"),
            lang_preference=preferences.get("lang_preference"),
            notification_settings=preferences.get("notification_settings"),
            app_settings=preferences.get("app_settings"),
        )
    )


@router.patch("/preferences")
async def update_user_preferences(
    preferences: UpdatePreferencesRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Update user preferences."""
    user_repo = get_user_repository()

    # Get or create user record
    user_record, _created = await user_repo.async_get_or_create_user(
        user["user_id"],
        username=user.get("username"),
        is_owner=False,
    )

    # Get current preferences or start with empty dict
    current_prefs = user_record.get("preferences_json") or {}

    # Update preferences
    updated_fields = []
    if preferences.lang_preference:
        current_prefs["lang_preference"] = preferences.lang_preference
        updated_fields.append("lang_preference")

    if preferences.notification_settings:
        if "notification_settings" not in current_prefs:
            current_prefs["notification_settings"] = {}
        current_prefs["notification_settings"].update(preferences.notification_settings)
        updated_fields.extend(
            [f"notification_settings.{k}" for k in preferences.notification_settings]
        )

    if preferences.app_settings:
        if "app_settings" not in current_prefs:
            current_prefs["app_settings"] = {}
        current_prefs["app_settings"].update(preferences.app_settings)
        updated_fields.extend([f"app_settings.{k}" for k in preferences.app_settings])

    # Save to database
    await user_repo.async_update_user_preferences(user["user_id"], current_prefs)
    profile_updates = _profile_updates_from_preferences(preferences)
    if profile_updates:
        await user_repo.async_update_user_profile(user["user_id"], **profile_updates)

    return success_response(
        PreferencesUpdateResult(
            updated_fields=updated_fields,
            updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    )


@router.get("/stats")
async def get_user_stats(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Get user statistics."""
    from collections import Counter
    from urllib.parse import urlparse

    user_repo = get_user_repository()
    summary_repo = get_summary_repository()

    # Get user summaries with pagination (using a large limit for stats)
    summaries_list, total_summaries, unread_count = await summary_repo.async_get_user_summaries(
        user_id=user["user_id"],
        limit=10000,  # Large limit for stats
        offset=0,
    )

    read_count = total_summaries - unread_count

    # Calculate reading time, favorite topics, and domains
    total_reading_time = 0
    topic_counter: Counter[str] = Counter()
    domain_counter: Counter[str] = Counter()
    en_count = 0
    ru_count = 0

    for summary in summaries_list:
        json_payload = ensure_mapping(summary.get("json_payload"))
        total_reading_time += json_payload.get("estimated_reading_time_min", 0) or 0

        # Count topic tags
        topic_tags = json_payload.get("topic_tags", [])
        if isinstance(topic_tags, list):
            for tag in topic_tags:
                if tag and isinstance(tag, str):
                    topic_counter[tag.lower()] += 1

        # Count domains (from metadata or request URL)
        metadata = ensure_mapping(json_payload.get("metadata"))
        domain = metadata.get("domain")

        # Try to get domain from request data if available
        request_data = summary.get("request") or {}
        if isinstance(request_data, dict):
            normalized_url = request_data.get("normalized_url")
            if not domain and normalized_url:
                try:
                    parsed = urlparse(normalized_url)
                    domain = parsed.netloc
                except ValueError:
                    domain = ""
                    logger.warning("url_domain_parse_failed", exc_info=True)

        if domain:
            domain_counter[domain] += 1

        # Language distribution
        lang = summary.get("lang", "")
        if lang == "en":
            en_count += 1
        elif lang == "ru":
            ru_count += 1

    average_reading_time = total_reading_time / total_summaries if total_summaries > 0 else 0

    # Get top topics and domains
    favorite_topics = [
        TopicStat(topic=tag, count=count) for tag, count in topic_counter.most_common(10)
    ]
    favorite_domains = [
        DomainStat(domain=domain, count=count) for domain, count in domain_counter.most_common(10)
    ]

    # Get user record
    user_record = await user_repo.async_get_user_by_telegram_id(user["user_id"])

    # Get most recent summary timestamp from summaries_list
    last_summary_at = None
    if summaries_list:
        # Summaries are sorted by created_at desc
        first_summary = summaries_list[0]
        request_data = first_summary.get("request") or {}
        if isinstance(request_data, dict):
            last_summary_at = safe_isoformat(request_data.get("created_at"))

    return success_response(
        UserStatsData(
            total_summaries=total_summaries,
            unread_count=unread_count,
            read_count=read_count,
            total_reading_time_min=total_reading_time,
            average_reading_time_min=round(average_reading_time, 1),
            favorite_topics=favorite_topics,
            favorite_domains=favorite_domains,
            language_distribution={"en": en_count, "ru": ru_count},
            joined_at=safe_isoformat(user_record.get("created_at")) if user_record else None,
            last_summary_at=last_summary_at,
        )
    )


@router.get("/goals")
async def list_goals(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """List all reading goals for the current user."""
    goal_dicts = await UserGoalService().list_goals(user_id=user["user_id"])
    return success_response({"goals": goal_dicts})


@router.post("/goals")
async def upsert_goal(
    body: CreateGoalRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Create or update a reading goal (one per goal_type+scope per user)."""
    payload = await UserGoalService().upsert_goal(user_id=user["user_id"], body=body)
    return success_response(payload)


@router.delete("/goals/{goal_type}")
async def delete_goal(
    goal_type: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Remove a global reading goal by type (legacy endpoint)."""
    await UserGoalService().delete_global_goal(user_id=user["user_id"], goal_type=goal_type)
    return success_response({"deleted": True})


@router.delete("/goals/by-id/{goal_id}")
async def delete_goal_by_id(
    goal_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Remove a reading goal by its UUID."""
    await UserGoalService().delete_goal_by_id(user_id=user["user_id"], goal_id=goal_id)
    return success_response({"deleted": True})


@router.get("/streak")
async def get_streak(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Compute and return the user's reading streak data."""
    data = await UserActivityService().get_streak_data(user_id=user["user_id"])
    return success_response(
        StreakResponse(
            current_streak=data["current_streak"],
            longest_streak=data["longest_streak"],
            last_activity_date=data["last_activity_date"],
            today_count=data["today_count"],
            week_count=data["week_count"],
            month_count=data["month_count"],
        )
    )


# ---------------------------------------------------------------------------
# Goal progress
# ---------------------------------------------------------------------------


@router.get("/goals/progress")
async def get_goal_progress(user: dict[str, Any] = Depends(get_current_user)) -> Any:
    """Return each goal with current progress."""
    progress = await UserGoalService().get_goal_progress(user_id=user["user_id"])
    return success_response({"progress": progress})
