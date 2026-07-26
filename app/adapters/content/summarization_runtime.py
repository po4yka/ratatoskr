"""Shared runtime and dependency composition for summarization services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.llm_summarizer_articles import LLMArticleGenerator
from app.adapters.content.llm_summarizer_cache import LLMSummaryCache
from app.adapters.content.llm_summarizer_insights import (
    LLMInsightsGenerator,
    insights_has_content,
)
from app.adapters.content.llm_summarizer_metadata import LLMSummaryMetadataHelper
from app.adapters.content.llm_summarizer_semantic import LLMSemanticHelper
from app.adapters.content.llm_summarizer_text import coerce_string_list, truncate_content_text
from app.adapters.content.search_context_enricher import SearchContextEnricher
from app.application.services.summarization.llm_response_workflow import LLMResponseWorkflow

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.application.ports.cache import CachePort
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
    )
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.application.services.topic_search import TopicSearchService
    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue


class SummarizationRuntime:
    """Composition root for shared summarization infrastructure."""

    def __init__(
        self,
        *,
        cfg: AppConfig,
        db: Database,
        openrouter: LLMClientProtocol,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict[str, Any]], None],
        sem: Callable[[], Any],
        topic_search: TopicSearchService | None = None,
        db_write_queue: DbWriteQueue | None = None,
        summary_repo: SummaryRepositoryPort | None = None,
        request_repo: RequestRepositoryPort | None = None,
        crawl_result_repo: CrawlResultRepositoryPort | None = None,
        llm_repo: LLMRepositoryPort | None = None,
        user_repo: UserRepositoryPort | None = None,
        cache: CachePort | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.openrouter = openrouter
        self.response_formatter = response_formatter
        self.audit = audit_func
        self.sem = sem
        self.topic_search = topic_search
        self.db_write_queue = db_write_queue

        if summary_repo is None:
            msg = "summary_repo must be provided by the DI layer"
            raise ValueError(msg)
        if request_repo is None:
            msg = "request_repo must be provided by the DI layer"
            raise ValueError(msg)
        if crawl_result_repo is None:
            msg = "crawl_result_repo must be provided by the DI layer"
            raise ValueError(msg)
        if llm_repo is None:
            msg = "llm_repo must be provided by the DI layer"
            raise ValueError(msg)
        if user_repo is None:
            msg = "user_repo must be provided by the DI layer"
            raise ValueError(msg)
        self.summary_repo = summary_repo
        self.request_repo = request_repo
        self.crawl_result_repo = crawl_result_repo

        self.workflow = LLMResponseWorkflow(
            cfg=cfg,
            db=db,
            llm_client=openrouter,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            db_write_queue=db_write_queue,
            summary_repo=summary_repo,
            request_repo=request_repo,
            llm_repo=llm_repo,
            user_repo=user_repo,
        )
        if cache is None:
            from app.infrastructure.cache.redis_cache import RedisCache

            cache = RedisCache(cfg)
        self.cache: CachePort = cache
        self.prompt_version = cfg.runtime.summary_prompt_version
        self.semantic_helper = LLMSemanticHelper()
        self.cache_helper = LLMSummaryCache(
            cache=self.cache,
            cfg=cfg,
            prompt_version=self.prompt_version,
            insights_has_content=insights_has_content,
        )
        self.insights_generator = LLMInsightsGenerator(
            cfg=cfg,
            openrouter=openrouter,
            workflow=self.workflow,
            summary_repo=self.summary_repo,
            cache_helper=self.cache_helper,
            sem=sem,
            coerce_string_list=coerce_string_list,
            truncate_content_text=truncate_content_text,
        )
        self.metadata_helper = LLMSummaryMetadataHelper(
            request_repo=self.request_repo,
            crawl_result_repo=self.crawl_result_repo,
            openrouter=openrouter,
            workflow=self.workflow,
            sem=sem,
            semantic_helper=self.semantic_helper,
        )
        self.article_generator = LLMArticleGenerator(
            cfg=cfg,
            openrouter=openrouter,
            workflow=self.workflow,
            cache_helper=self.cache_helper,
            sem=sem,
            select_max_tokens=self.insights_generator.select_max_tokens,
            coerce_string_list=coerce_string_list,
        )
        self.search_enricher = SearchContextEnricher(
            cfg=cfg,
            openrouter=openrouter,
            topic_search=topic_search,
            llm_repo=llm_repo,
        )

    async def aclose(self, timeout: float = 5.0) -> None:
        """Drain background workflow tasks."""
        await self.workflow.aclose(timeout=timeout)
