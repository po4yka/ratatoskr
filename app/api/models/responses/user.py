"""User-facing API response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .common import SuccessResponse


class PreferencesData(BaseModel):
    user_id: int = Field(serialization_alias="userId")
    telegram_username: str | None = Field(default=None, serialization_alias="telegramUsername")
    lang_preference: str | None = Field(default=None, serialization_alias="langPreference")
    notification_settings: dict[str, Any] | None = Field(
        default=None, serialization_alias="notificationSettings"
    )
    app_settings: dict[str, Any] | None = Field(default=None, serialization_alias="appSettings")


class PreferencesUpdateResult(BaseModel):
    updated_fields: list[str] = Field(serialization_alias="updatedFields")
    updated_at: str = Field(serialization_alias="updatedAt")


class UserProfileResponse(BaseModel):
    user_id: int = Field(serialization_alias="userId")
    telegram_username: str | None = Field(default=None, serialization_alias="telegramUsername")
    display_name: str | None = Field(default=None, serialization_alias="displayName")
    locale: str
    theme: str
    default_summary_language: str = Field(serialization_alias="defaultSummaryLanguage")
    onboarding_completed_at: str | None = Field(
        default=None, serialization_alias="onboardingCompletedAt"
    )
    created_at: str | None = Field(default=None, serialization_alias="createdAt")
    updated_at: str | None = Field(default=None, serialization_alias="updatedAt")


class UserMeResponse(BaseModel):
    profile: UserProfileResponse


class UserFeedTokenResponse(BaseModel):
    token: str
    feed_url: str = Field(serialization_alias="feedUrl")


class UserFeedTokenRevocationResponse(BaseModel):
    revoked: bool


class UserFeedTokenSuccessResponse(SuccessResponse):
    data: UserFeedTokenResponse


class UserFeedTokenRevocationSuccessResponse(SuccessResponse):
    data: UserFeedTokenRevocationResponse


class TopicStat(BaseModel):
    topic: str
    count: int


class DomainStat(BaseModel):
    domain: str
    count: int


class UserStatsData(BaseModel):
    total_summaries: int = Field(serialization_alias="totalSummaries")
    unread_count: int = Field(serialization_alias="unreadCount")
    read_count: int = Field(serialization_alias="readCount")
    total_reading_time_min: int = Field(serialization_alias="totalReadingTimeMin")
    average_reading_time_min: float = Field(serialization_alias="averageReadingTimeMin")
    favorite_topics: list[TopicStat] = Field(serialization_alias="favoriteTopics")
    favorite_domains: list[DomainStat] = Field(serialization_alias="favoriteDomains")
    language_distribution: dict[str, int] = Field(serialization_alias="languageDistribution")
    joined_at: str | None = Field(default=None, serialization_alias="joinedAt")
    last_summary_at: str | None = Field(default=None, serialization_alias="lastSummaryAt")


class SessionListResponse(BaseModel):
    sessions: list[Any]


class HighlightResponse(BaseModel):
    id: str
    summary_id: str = Field(serialization_alias="summaryId")
    text: str
    start_offset: int | None = Field(default=None, serialization_alias="startOffset")
    end_offset: int | None = Field(default=None, serialization_alias="endOffset")
    color: str | None = None
    note: str | None = None
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class HighlightListResponse(BaseModel):
    highlights: list[HighlightResponse]


class TagResponse(BaseModel):
    id: int
    name: str
    color: str | None = None
    summary_count: int = Field(default=0, serialization_alias="summaryCount")
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class TagListResponse(BaseModel):
    tags: list[TagResponse]


class GoalResponse(BaseModel):
    id: str = Field(serialization_alias="id")
    goal_type: str = Field(serialization_alias="goalType")
    target_count: int = Field(serialization_alias="targetCount")
    scope_type: str = Field(default="global", serialization_alias="scopeType")
    scope_id: int | None = Field(default=None, serialization_alias="scopeId")
    scope_name: str | None = Field(default=None, serialization_alias="scopeName")
    created_at: str = Field(serialization_alias="createdAt")
    updated_at: str = Field(serialization_alias="updatedAt")


class GoalProgressResponse(BaseModel):
    goal_type: str = Field(serialization_alias="goalType")
    target_count: int = Field(serialization_alias="targetCount")
    current_count: int = Field(serialization_alias="currentCount")
    achieved: bool
    scope_type: str = Field(default="global", serialization_alias="scopeType")
    scope_id: int | None = Field(default=None, serialization_alias="scopeId")
    scope_name: str | None = Field(default=None, serialization_alias="scopeName")


class StreakResponse(BaseModel):
    current_streak: int = Field(serialization_alias="currentStreak")
    longest_streak: int = Field(serialization_alias="longestStreak")
    last_activity_date: str | None = Field(default=None, serialization_alias="lastActivityDate")
    today_count: int = Field(serialization_alias="todayCount")
    week_count: int = Field(serialization_alias="weekCount")
    month_count: int = Field(serialization_alias="monthCount")
