"""Pluggable proactive source ingestors."""

from app.adapters.ingestors.hn import HackerNewsIngester
from app.adapters.ingestors.reddit import RedditIngester
from app.adapters.ingestors.registry import SOURCE_INGESTER_DESCRIPTORS, create_source_ingesters
from app.adapters.ingestors.runner import SourceIngestionRunner
from app.adapters.ingestors.twitter import TwitterIngester, TwitterIngestionConfig

__all__ = [
    "SOURCE_INGESTER_DESCRIPTORS",
    "HackerNewsIngester",
    "RedditIngester",
    "SourceIngestionRunner",
    "TwitterIngester",
    "TwitterIngestionConfig",
    "create_source_ingesters",
]
