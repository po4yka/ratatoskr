from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.services.summary_embedding_generator import SummaryEmbeddingGenerator
from app.application.services.topic_search import LocalTopicSearchService, TopicSearchService
from app.core.embedding_space import resolve_embedding_space_identifier
from app.core.logging_utils import get_logger
from app.di.repositories import (
    build_embedding_repository,
    build_request_repository,
    build_summary_repository,
    build_topic_search_repository,
)
from app.di.types import SearchDependencies
from app.infrastructure.embedding.embedding_factory import create_embedding_service
from app.infrastructure.search.hybrid_search_service import HybridSearchService
from app.infrastructure.search.query_expansion_service import QueryExpansionService
from app.infrastructure.search.reranking_service import OpenRouterRerankingService

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)

DEFAULT_TOPIC_SEARCH_MAX_RESULTS = 5


def get_topic_search_limit(cfg: AppConfig) -> int:
    raw_value = cfg.runtime.topic_search_max_results
    try:
        limit = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("topic_search_limit_invalid", extra={"value": raw_value})
        return DEFAULT_TOPIC_SEARCH_MAX_RESULTS

    if limit <= 0:
        logger.warning("topic_search_limit_non_positive", extra={"value": limit})
        return DEFAULT_TOPIC_SEARCH_MAX_RESULTS

    if limit > 10:
        logger.warning("topic_search_limit_too_large", extra={"value": limit})
        return 10

    return limit


def build_search_dependencies(
    cfg: AppConfig,
    db: Database,
    *,
    llm_client: Any,
    audit_func: Callable[[str, str, dict[str, Any]], None],
    firecrawl_client: Any | None = None,
    topic_search_max_results: int | None = None,
) -> SearchDependencies:
    """Build the shared local, vector, and hybrid search stack."""
    max_results = topic_search_max_results or get_topic_search_limit(cfg)
    vector_store: Any | None = None

    try:
        from app.infrastructure.vector.qdrant_store import QdrantVectorStore

        vector_store = QdrantVectorStore(
            url=cfg.vector_store.url,
            api_key=cfg.vector_store.api_key,
            environment=cfg.vector_store.environment,
            user_scope=cfg.vector_store.user_scope,
            collection_version=cfg.vector_store.collection_version,
            embedding_space=resolve_embedding_space_identifier(cfg.embedding),
            embedding_dim=cfg.embedding.embedding_dim,
            required=cfg.vector_store.required,
            connection_timeout=cfg.vector_store.connection_timeout,
        )
        if not vector_store.available:
            logger.warning(
                "vector_not_available_continuing",
                extra={"url": cfg.vector_store.url},
            )
    except Exception as exc:
        if cfg.vector_store.required:
            raise
        logger.warning(
            "vector_init_failed_continuing_without_vector_search",
            extra={"error": str(exc), "url": cfg.vector_store.url},
        )
        vector_store = None

    local_searcher = LocalTopicSearchService(
        repository=build_topic_search_repository(db),
        max_results=max_results,
        audit_func=audit_func,
    )
    topic_searcher = (
        TopicSearchService(
            firecrawl=firecrawl_client,
            max_results=max_results,
            audit_func=audit_func,
        )
        if firecrawl_client is not None
        else None
    )
    # app_config wires the Redis EmbeddingCache in (when enabled): this single
    # service instance backs both the summary-embedding write path and the vector
    # query read path below, so caching covers search + RAG here.
    embedding_service = create_embedding_service(cfg.embedding, app_config=cfg)
    embedding_generator = SummaryEmbeddingGenerator(
        embedding_repository=build_embedding_repository(db),
        request_repository=build_request_repository(db),
        summary_repository=build_summary_repository(db),
        embedding_service=embedding_service,
        max_token_length=cfg.embedding.max_token_length,
    )
    query_expansion_service = QueryExpansionService(max_expansions=5, use_synonyms=True)

    vector_search_service: Any | None = None
    if vector_store is not None:
        from app.infrastructure.search.vector_search_service import StoreVectorSearchService

        vector_search_service = StoreVectorSearchService(
            vector_store=vector_store,
            embedding_service=embedding_service,
            default_top_k=max_results * 2,
        )

    reranking_service = OpenRouterRerankingService(
        client=llm_client,
        top_k=max_results * 2,
        timeout_sec=cfg.runtime.request_timeout_sec,
    )
    hybrid_search_service = HybridSearchService(
        fts_service=local_searcher,
        vector_service=vector_search_service,
        fts_weight=1.0 if vector_search_service is None else 0.4,
        vector_weight=0.0 if vector_search_service is None else 0.6,
        max_results=max_results,
        query_expansion=query_expansion_service,
        reranking=reranking_service,
    )

    return SearchDependencies(
        local_searcher=local_searcher,
        topic_searcher=topic_searcher,
        embedding_service=embedding_service,
        embedding_generator=embedding_generator,
        vector_store=vector_store,
        vector_search_service=vector_search_service,
        hybrid_search_service=hybrid_search_service,
        query_expansion_service=query_expansion_service,
    )
