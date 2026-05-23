"""Static registry for proactive source ingestors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.ports.source_ingestors import (
    SourceIngester,
    SourceIngesterBuildContext,
    SourceIngesterDescriptor,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.config.signal_ingestion import SignalIngestionConfig


def _build_hacker_news_ingesters(
    cfg: SignalIngestionConfig,
    context: SourceIngesterBuildContext,
) -> Iterable[SourceIngester]:
    del context
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


def _build_reddit_ingesters(
    cfg: SignalIngestionConfig,
    context: SourceIngesterBuildContext,
) -> Iterable[SourceIngester]:
    del context
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


def _build_twitter_ingesters(
    cfg: SignalIngestionConfig,
    context: SourceIngesterBuildContext,
) -> Iterable[SourceIngester]:
    del context
    from app.adapters.ingestors.twitter import TwitterIngester, TwitterIngestionConfig

    return (
        TwitterIngester(
            TwitterIngestionConfig(
                enabled=cfg.enabled and cfg.twitter_enabled,
                ack_cost=cfg.twitter_ack_cost,
            )
        ),
    )


def _build_x_timeline_ingesters(
    cfg: SignalIngestionConfig,
    context: SourceIngesterBuildContext,
) -> Iterable[SourceIngester]:
    if not (cfg.enabled and cfg.social_x_ingestion_enabled):
        return ()
    if context.social_token_resolver is None:
        return ()

    from app.adapters.ingestors.x_timeline import XTimelineIngester, XTimelineIngestionConfig

    return tuple(
        XTimelineIngester(
            config=XTimelineIngestionConfig(
                enabled=True,
                user_id=user_id,
                timeline_mode=cfg.social_x_timeline_mode,
                limit=cfg.max_items_per_source,
                api_base_url=context.x_api_base_url,
            ),
            token_resolver=context.social_token_resolver,
            social_connection_repository=context.social_connection_repository,
        )
        for user_id in context.subscriber_user_ids
    )


def _build_threads_user_threads_ingesters(
    cfg: SignalIngestionConfig,
    context: SourceIngesterBuildContext,
) -> Iterable[SourceIngester]:
    if not (cfg.enabled and cfg.social_threads_ingestion_enabled):
        return ()
    if context.social_token_resolver is None:
        return ()

    from app.adapters.ingestors.threads_user_threads import (
        ThreadsUserThreadsIngester,
        ThreadsUserThreadsIngestionConfig,
    )

    return tuple(
        ThreadsUserThreadsIngester(
            config=ThreadsUserThreadsIngestionConfig(
                enabled=True,
                user_id=user_id,
                limit=cfg.max_items_per_source,
                graph_base_url=context.threads_graph_base_url,
            ),
            token_resolver=context.social_token_resolver,
            social_connection_repository=context.social_connection_repository,
        )
        for user_id in context.subscriber_user_ids
    )


SOURCE_INGESTER_DESCRIPTORS: tuple[SourceIngesterDescriptor, ...] = (
    SourceIngesterDescriptor(name="hacker_news", build=_build_hacker_news_ingesters),
    SourceIngesterDescriptor(name="reddit", build=_build_reddit_ingesters),
    SourceIngesterDescriptor(name="twitter", build=_build_twitter_ingesters),
    SourceIngesterDescriptor(name="x_timeline", build=_build_x_timeline_ingesters),
    SourceIngesterDescriptor(
        name="threads_user_threads",
        build=_build_threads_user_threads_ingesters,
    ),
)


def create_source_ingesters(
    cfg: SignalIngestionConfig,
    *,
    descriptors: Iterable[SourceIngesterDescriptor] = SOURCE_INGESTER_DESCRIPTORS,
    context: SourceIngesterBuildContext | None = None,
) -> list[SourceIngester]:
    """Build configured source ingesters from the static descriptor registry."""
    context = context or SourceIngesterBuildContext()
    ingesters: list[SourceIngester] = []
    for descriptor in descriptors:
        ingesters.extend(descriptor.build(cfg, context))
    return ingesters
