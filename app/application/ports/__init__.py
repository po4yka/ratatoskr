"""Compatibility facade for application-layer ports.

Production code must import from specific submodules under
``app.application.ports``. This root facade exists only for tests and
incremental compatibility while the import surface is tightened.
"""

from __future__ import annotations

from .aggregation_sessions import AggregationSessionRepositoryPort
from .audio import AudioGenerationRepositoryPort, AudioStoragePort, TTSProviderPort
from .audit import AuditLogRepositoryPort
from .backups import BackupRepositoryPort
from .batch_sessions import BatchSessionRepositoryPort
from .imports import BookmarkImportPort, ImportJobRepositoryPort
from .requests import (
    CrawlResultRepositoryPort,
    LLMCallRecord,
    LLMRepositoryPort,
    RequestRepositoryPort,
    VideoDownloadRepositoryPort,
)
from .repository_analysis import RepositoryAnalysisRecord, RepositoryAnalysisRepositoryPort
from .rules import (
    CollectionMembershipPort,
    RuleContextPort,
    RuleRateLimiterPort,
    RuleRepositoryPort,
    WebhookDispatchPort,
    WebhookRepositoryPort,
)
from .search import (
    EmbeddingProviderPort,
    EmbeddingRepositoryPort,
    TopicSearchClientPort,
    TopicSearchRepositoryPort,
    TopicSearchResultItemPort,
    TopicSearchResultPort,
    VectorSearchPort,
)
from .signal_sources import SignalSourceRepositoryPort
from .social_connections import (
    SUPPORTED_SOCIAL_PROVIDERS,
    SocialConnectionRecord,
    SocialConnectionRepositoryPort,
    SocialConnectionUpdate,
    SocialConnectionUpsert,
    SocialFetchAttemptCreate,
)
from .summaries import SummaryRepositoryPort, TagRepositoryPort
from .transcriptions import (
    TranscriptionArtifactCreate,
    TranscriptionArtifactRecord,
    TranscriptionJobCreate,
    TranscriptionJobRecord,
    TranscriptionRepositoryPort,
)
from .users import UserRepositoryPort

__all__ = [
    "SUPPORTED_SOCIAL_PROVIDERS",
    "AggregationSessionRepositoryPort",
    "AudioGenerationRepositoryPort",
    "AudioStoragePort",
    "AuditLogRepositoryPort",
    "BackupRepositoryPort",
    "BatchSessionRepositoryPort",
    "BookmarkImportPort",
    "CollectionMembershipPort",
    "CrawlResultRepositoryPort",
    "EmbeddingProviderPort",
    "EmbeddingRepositoryPort",
    "ImportJobRepositoryPort",
    "LLMCallRecord",
    "LLMRepositoryPort",
    "RepositoryAnalysisRecord",
    "RepositoryAnalysisRepositoryPort",
    "RequestRepositoryPort",
    "RuleContextPort",
    "RuleRateLimiterPort",
    "RuleRepositoryPort",
    "SignalSourceRepositoryPort",
    "SocialConnectionRecord",
    "SocialConnectionRepositoryPort",
    "SocialConnectionUpdate",
    "SocialConnectionUpsert",
    "SocialFetchAttemptCreate",
    "SummaryRepositoryPort",
    "TTSProviderPort",
    "TagRepositoryPort",
    "TopicSearchClientPort",
    "TopicSearchRepositoryPort",
    "TopicSearchResultItemPort",
    "TopicSearchResultPort",
    "TranscriptionArtifactCreate",
    "TranscriptionArtifactRecord",
    "TranscriptionJobCreate",
    "TranscriptionJobRecord",
    "TranscriptionRepositoryPort",
    "UserRepositoryPort",
    "VectorSearchPort",
    "VideoDownloadRepositoryPort",
    "WebhookDispatchPort",
    "WebhookRepositoryPort",
]
