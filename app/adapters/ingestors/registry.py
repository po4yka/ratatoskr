"""Static registry for proactive source ingestors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.ports.source_ingestors import SourceIngester, SourceIngesterDescriptor

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.config.signal_ingestion import SignalIngestionConfig


def _build_hacker_news_ingesters(cfg: SignalIngestionConfig) -> Iterable[SourceIngester]:
    if not (cfg.enabled and cfg.hn_enabled):
        return ()

    from app.adapters.ingestors.hn import HackerNewsIngester

    return tuple(
        HackerNewsIngester(
            feed=feed,
            limit=cfg.max_items_per_source,
            enabled=True,
        )
        for feed in cfg.hn_feed_names()
    )


def _build_reddit_ingesters(cfg: SignalIngestionConfig) -> Iterable[SourceIngester]:
    if not (cfg.enabled and cfg.reddit_enabled):
        return ()

    from app.adapters.ingestors.reddit import RedditIngester, RequestRateBudget

    reddit_rate_budget = RequestRateBudget(max_requests_per_minute=cfg.reddit_requests_per_minute)
    return tuple(
        RedditIngester(
            subreddit=subreddit,
            listing=cfg.reddit_listing,
            limit=cfg.max_items_per_source,
            enabled=True,
            rate_budget=reddit_rate_budget,
        )
        for subreddit in cfg.reddit_names()
    )


def _build_twitter_ingesters(cfg: SignalIngestionConfig) -> Iterable[SourceIngester]:
    from app.adapters.ingestors.twitter import TwitterIngester, TwitterIngestionConfig

    return (
        TwitterIngester(
            TwitterIngestionConfig(
                enabled=cfg.enabled and cfg.twitter_enabled,
                ack_cost=cfg.twitter_ack_cost,
            )
        ),
    )


SOURCE_INGESTER_DESCRIPTORS: tuple[SourceIngesterDescriptor, ...] = (
    SourceIngesterDescriptor(name="hacker_news", build=_build_hacker_news_ingesters),
    SourceIngesterDescriptor(name="reddit", build=_build_reddit_ingesters),
    SourceIngesterDescriptor(name="twitter", build=_build_twitter_ingesters),
)


def create_source_ingesters(
    cfg: SignalIngestionConfig,
    *,
    descriptors: Iterable[SourceIngesterDescriptor] = SOURCE_INGESTER_DESCRIPTORS,
) -> list[SourceIngester]:
    """Build configured source ingesters from the static descriptor registry."""
    ingesters: list[SourceIngester] = []
    for descriptor in descriptors:
        ingesters.extend(descriptor.build(cfg))
    return ingesters
