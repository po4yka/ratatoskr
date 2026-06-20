from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.infrastructure.persistence.repositories.aggregation_session_repository import (
    AggregationSessionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.audit_log_repository import (
    AuditLogRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.backup_repository import (
    BackupRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.batch_session_repository import (
    BatchSessionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.collection_repository import (
    CollectionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.crawl_result_repository import (
    CrawlResultRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.embedding_repository import (
    EmbeddingRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.import_job_repository import (
    ImportJobRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.llm_repository import (
    LLMRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.rule_repository import (
    RuleRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.social_connection_repository import (
    SocialConnectionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.summary_repository import (
    SummaryRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.tag_repository import (
    TagRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.topic_search_repository import (
    TopicSearchRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.transcription_repository import (
    TranscriptionRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.user_content_repository import (
    UserContentRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.user_repository import (
    UserRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.video_download_repository import (
    VideoDownloadRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.webhook_repository import (
    WebhookRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.application.ports.aggregation_sessions import AggregationSessionRepositoryPort
    from app.application.ports.audit import AuditLogRepositoryPort
    from app.application.ports.backups import BackupRepositoryPort
    from app.application.ports.batch_sessions import BatchSessionRepositoryPort
    from app.application.ports.imports import ImportJobRepositoryPort
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
        VideoDownloadRepositoryPort,
    )
    from app.application.ports.rules import RuleRepositoryPort, WebhookRepositoryPort
    from app.application.ports.search import EmbeddingRepositoryPort, TopicSearchRepositoryPort
    from app.application.ports.social_connections import SocialConnectionRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort, TagRepositoryPort
    from app.application.ports.transcriptions import TranscriptionRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.db.session import Database


def build_request_repository(db: Database) -> RequestRepositoryPort:
    return RequestRepositoryAdapter(db)


def build_aggregation_session_repository(
    db: Database,
) -> AggregationSessionRepositoryPort:
    return AggregationSessionRepositoryAdapter(db)


def build_summary_repository(db: Database) -> SummaryRepositoryPort:
    return SummaryRepositoryAdapter(db)


def build_user_repository(db: Database) -> UserRepositoryPort:
    return UserRepositoryAdapter(db)


def build_user_content_repository(db: Database) -> Any:
    return UserContentRepositoryAdapter(db)


def build_llm_repository(db: Database) -> LLMRepositoryPort:
    return LLMRepositoryAdapter(db)


def build_crawl_result_repository(db: Database) -> CrawlResultRepositoryPort:
    return CrawlResultRepositoryAdapter(db)


def build_collection_repository(db: Database) -> Any:
    return CollectionRepositoryAdapter(db)


def build_video_download_repository(db: Database) -> VideoDownloadRepositoryPort:
    return VideoDownloadRepositoryAdapter(db)


def build_audit_log_repository(db: Database) -> AuditLogRepositoryPort:
    return AuditLogRepositoryAdapter(db)


def build_batch_session_repository(db: Database) -> BatchSessionRepositoryPort:
    return BatchSessionRepositoryAdapter(db)


def build_topic_search_repository(db: Database) -> TopicSearchRepositoryPort:
    return TopicSearchRepositoryAdapter(db)


def build_embedding_repository(db: Database) -> EmbeddingRepositoryPort:
    return EmbeddingRepositoryAdapter(db)


def build_tag_repository(db: Database) -> TagRepositoryPort:
    return TagRepositoryAdapter(db)


def build_import_job_repository(db: Database) -> ImportJobRepositoryPort:
    return ImportJobRepositoryAdapter(db)


def build_rule_repository(db: Database) -> RuleRepositoryPort:
    return RuleRepositoryAdapter(db)


def build_webhook_repository(db: Database) -> WebhookRepositoryPort:
    return WebhookRepositoryAdapter(db)


def build_backup_repository(db: Database) -> BackupRepositoryPort:
    return BackupRepositoryAdapter(db)


def build_social_connection_repository(db: Database) -> SocialConnectionRepositoryPort:
    return SocialConnectionRepositoryAdapter(db)


def build_transcription_repository(db: Database) -> TranscriptionRepositoryPort:
    return TranscriptionRepositoryAdapter(db)


def build_rss_feed_repository(db: Database) -> Any:
    from app.infrastructure.persistence.repositories.rss_feed_repository import (
        RSSFeedRepositoryAdapter,
    )

    return RSSFeedRepositoryAdapter(db)
