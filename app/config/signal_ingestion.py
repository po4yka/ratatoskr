"""Optional source-ingestion configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SignalIngestionConfig(BaseModel):
    """Configuration for optional proactive source ingestors."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(default=False, validation_alias="SIGNAL_INGESTION_ENABLED")
    hn_enabled: bool = Field(default=False, validation_alias="SIGNAL_HN_ENABLED")
    hn_feeds: str = Field(default="top", validation_alias="SIGNAL_HN_FEEDS")
    reddit_enabled: bool = Field(default=False, validation_alias="SIGNAL_REDDIT_ENABLED")
    reddit_subreddits: str = Field(default="", validation_alias="SIGNAL_REDDIT_SUBREDDITS")
    reddit_listing: str = Field(default="hot", validation_alias="SIGNAL_REDDIT_LISTING")
    reddit_requests_per_minute: int = Field(
        default=60,
        validation_alias="SIGNAL_REDDIT_REQUESTS_PER_MINUTE",
        ge=1,
        le=100,
    )
    max_items_per_source: int = Field(
        default=30,
        validation_alias="SIGNAL_MAX_ITEMS_PER_SOURCE",
        ge=1,
        le=100,
    )
    twitter_enabled: bool = Field(default=False, validation_alias="TWITTER_INGESTION_ENABLED")
    twitter_ack_cost: bool = Field(
        default=False,
        validation_alias="TWITTER_INGESTION_ACK_COST",
    )
    social_x_ingestion_enabled: bool = Field(
        default=False,
        validation_alias="SOCIAL_X_INGESTION_ENABLED",
    )
    social_x_timeline_mode: str = Field(
        default="user_posts",
        validation_alias="SOCIAL_X_TIMELINE_MODE",
    )
    social_threads_ingestion_enabled: bool = Field(
        default=False,
        validation_alias="SOCIAL_THREADS_INGESTION_ENABLED",
    )

    @field_validator("hn_feeds", "reddit_subreddits", mode="before")
    @classmethod
    def _normalize_csv(cls, value: object) -> str:
        if isinstance(value, list | tuple):
            return ",".join(str(item).strip() for item in value if str(item).strip())
        return str(value or "").strip()

    @field_validator("social_x_timeline_mode", mode="before")
    @classmethod
    def _normalize_x_timeline_mode(cls, value: object) -> str:
        mode = str(value or "user_posts").strip().lower().replace("-", "_")
        if mode not in {"user_posts", "home_timeline"}:
            msg = "SOCIAL_X_TIMELINE_MODE must be user_posts or home_timeline"
            raise ValueError(msg)
        return mode

    def hn_feed_names(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.hn_feeds.split(",") if part.strip())

    def reddit_names(self) -> tuple[str, ...]:
        return tuple(
            part.strip().removeprefix("r/")
            for part in self.reddit_subreddits.split(",")
            if part.strip()
        )

    @property
    def any_enabled(self) -> bool:
        return self.enabled and (
            self.hn_enabled
            or (self.reddit_enabled and bool(self.reddit_names()))
            or self.twitter_enabled
            or self.social_x_ingestion_enabled
            or self.social_threads_ingestion_enabled
        )
