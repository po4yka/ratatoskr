"""RSS feed configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RSSConfig(BaseModel):
    """Configuration for the RSS feed subscription and auto-summarization subsystem."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(default=False, validation_alias="RSS_ENABLED")
    poll_interval_minutes: int = Field(
        default=30,
        validation_alias="RSS_POLL_INTERVAL_MINUTES",
        ge=5,
        le=1440,
        description="How often to poll feeds (minutes)",
    )
    auto_summarize: bool = Field(
        default=True,
        validation_alias="RSS_AUTO_SUMMARIZE",
        description="Automatically summarize and deliver new items",
    )
    min_content_length: int = Field(
        default=500,
        validation_alias="RSS_MIN_CONTENT_LENGTH",
        ge=0,
        le=10000,
        description="Min chars in RSS content to use inline (skip scraping)",
    )
    max_items_per_poll: int = Field(
        default=20,
        validation_alias="RSS_MAX_ITEMS_PER_POLL",
        ge=1,
        le=100,
        description="Safety cap on items processed per poll cycle",
    )
    max_feeds_per_poll: int = Field(
        default=200,
        validation_alias="RSS_MAX_FEEDS_PER_POLL",
        ge=1,
        le=5000,
        description="Safety cap on active feeds loaded per poll cycle "
        "(least-recently-fetched first, so feeds rotate across cycles)",
    )
    poll_concurrency: int = Field(
        default=8,
        validation_alias="RSS_POLL_CONCURRENCY",
        ge=1,
        le=32,
        description="Maximum feeds polled concurrently; requests to one host remain serialized",
    )
    concurrency: int = Field(
        default=2,
        validation_alias="RSS_CONCURRENCY",
        ge=1,
        le=10,
        description="Parallel LLM summarization calls",
    )
    scrape_short_content: bool = Field(
        default=False,
        validation_alias="RSS_SCRAPE_SHORT_CONTENT",
        description="Scrape full article via scraper chain when inline content is too short",
    )
