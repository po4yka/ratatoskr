from __future__ import annotations

from app.adapters.ingestors.registry import SOURCE_INGESTER_DESCRIPTORS, create_source_ingesters
from app.application.ports.source_ingestors import (
    IngestedSource,
    SourceFetchResult,
    SourceIngesterDescriptor,
)
from app.config.signal_ingestion import SignalIngestionConfig


class _FakeIngester:
    name = "fake"

    def is_enabled(self) -> bool:
        return True

    def source_identity(self) -> IngestedSource:
        return IngestedSource(kind="fake", external_id="fake:one")

    async def fetch(self) -> SourceFetchResult:
        return SourceFetchResult(source=self.source_identity())


def test_fake_ingestor_descriptor_is_additive() -> None:
    cfg = SignalIngestionConfig()
    fake = _FakeIngester()
    descriptors = (SourceIngesterDescriptor(name="fake", build=lambda _cfg: (fake,)),)

    assert create_source_ingesters(cfg, descriptors=descriptors) == [fake]


def test_static_registry_preserves_current_source_families() -> None:
    assert [descriptor.name for descriptor in SOURCE_INGESTER_DESCRIPTORS] == [
        "hacker_news",
        "reddit",
        "twitter",
    ]


def test_registry_builds_hn_reddit_and_twitter_from_config() -> None:
    cfg = SignalIngestionConfig(
        enabled=True,
        hn_enabled=True,
        hn_feeds="top,new",
        reddit_enabled=True,
        reddit_subreddits="python,selfhosted",
        reddit_listing="hot",
        twitter_enabled=True,
        twitter_ack_cost=True,
    )

    ingesters = create_source_ingesters(cfg)

    assert [ingester.name for ingester in ingesters] == [
        "hacker_news:top",
        "hacker_news:new",
        "reddit:python:hot",
        "reddit:selfhosted:hot",
        "twitter",
    ]
    assert all(ingester.is_enabled() for ingester in ingesters)


def test_registry_preserves_disabled_twitter_placeholder_behavior() -> None:
    cfg = SignalIngestionConfig(enabled=False, hn_enabled=True, twitter_enabled=True)

    ingesters = create_source_ingesters(cfg)

    assert [ingester.name for ingester in ingesters] == ["twitter"]
    assert ingesters[0].is_enabled() is False
