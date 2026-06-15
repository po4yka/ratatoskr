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
from .cache import CachePort
from .extraction import ExtractionPort
from .imports import BookmarkImportPort, ImportJobRepositoryPort
from .message_persistence import MessagePersistencePort
from .repository_analysis import RepositoryAnalysisRecord, RepositoryAnalysisRepositoryPort
from .requests import (
    CrawlResultRepositoryPort,
    LLMCallRecord,
    LLMRepositoryPort,
    RequestRepositoryPort,
    VideoDownloadRepositoryPort,
)
from .retrieval import RetrievalPort
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
from .stream_sink import StreamSinkPort
from .summaries import SummaryRepositoryPort, TagRepositoryPort
from .transcriptions import (
    LeasedTranscriptionJob,
    TranscriptionArtifactCreate,
    TranscriptionArtifactRecord,
    TranscriptionJobCreate,
    TranscriptionJobRecord,
    TranscriptionProgressEventRecord,
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
    "CachePort",
    "CollectionMembershipPort",
    "CrawlResultRepositoryPort",
    "EmbeddingProviderPort",
    "EmbeddingRepositoryPort",
    "ExtractionPort",
    "ImportJobRepositoryPort",
    "LLMCallRecord",
    "LLMRepositoryPort",
    "LeasedTranscriptionJob",
    "MessagePersistencePort",
    "RepositoryAnalysisRecord",
    "RepositoryAnalysisRepositoryPort",
    "RequestRepositoryPort",
    "RetrievalPort",
    "RuleContextPort",
    "RuleRateLimiterPort",
    "RuleRepositoryPort",
    "SignalSourceRepositoryPort",
    "SocialConnectionRecord",
    "SocialConnectionRepositoryPort",
    "SocialConnectionUpdate",
    "SocialConnectionUpsert",
    "SocialFetchAttemptCreate",
    "StreamSinkPort",
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
    "TranscriptionProgressEventRecord",
    "TranscriptionRepositoryPort",
    "UserRepositoryPort",
    "VectorSearchPort",
    "VideoDownloadRepositoryPort",
    "WebhookDispatchPort",
    "WebhookRepositoryPort",
]
