"""Composition of the add-repository flow for runtimes outside the Mobile API.

The API assembles this use case from bundles it already has on hand
(``app.di.api``). The Telegram bot has the same pieces under different names, so
this module takes the primitives directly and does the wiring once, rather than
letting each runtime infer it -- two runtimes inferring the same composition
independently is how they drift apart.

The suggester comes from :mod:`app.di.star_lists`, the same factory the API and
the nightly filing pass use, so ``/star`` files a repository exactly where the
other two paths would have filed it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.config import AppConfig
    from app.db.session import Database


def build_add_repository_use_case(
    *,
    cfg: AppConfig,
    db: Database,
    scraper: Any,
    response_formatter: Any,
    audit_func: Callable[[str, str, dict[str, Any]], None],
    sem: Callable[[], Any],
    llm_client: Any,
    llm_repository: Any = None,
    vector_store: Any = None,
    embedding_service: Any = None,
) -> Any:
    """Build ``AddRepositoryUseCase`` from primitives a bot runtime already holds.

    ``vector_store`` and ``embedding_service`` are optional: without them there is
    no suggester, and ``mode=star`` still stars and enrols the repository, it just
    reports that nothing filed it. That is the same degradation the API applies,
    and it is reported rather than hidden.
    """
    from app.adapters.git_backup.mirror_enrollment_adapter import GitMirrorEnrollmentAdapter
    from app.adapters.git_backup.repository import GitMirrorRepository
    from app.adapters.github.github_graphql_client import GitHubGraphQLClient
    from app.adapters.github.repository_ingest_adapter import GitHubRepositoryIngestAdapter
    from app.application.use_cases.add_repository import AddRepositoryUseCase
    from app.application.use_cases.manage_star_lists import ManageStarListsUseCase
    from app.di.platform_extractors import build_github_platform_extractor
    from app.di.star_lists import build_star_list_suggester
    from app.infrastructure.persistence.repositories.github_integration_repository import (
        GitHubIntegrationRepository,
    )
    from app.infrastructure.persistence.repositories.repository_read_repository import (
        RepositoryReadRepositoryAdapter,
    )

    # Hand the runtime's own embedding stack to the factory when it has one, so a
    # bot process does not end up holding a second embedding model and Qdrant
    # client purely to ingest a repository. Without one the factory builds its own.
    embedding_gen: Any | None = None
    if embedding_service is not None:
        from app.infrastructure.embedding.repository_embedding import (
            RepositoryEmbeddingGenerator,
        )

        embedding_gen = RepositoryEmbeddingGenerator(
            embedding_service=embedding_service,
            qdrant_store=vector_store,
            db=db,
            environment=cfg.vector_store.environment,
            user_scope=cfg.vector_store.user_scope,
        )

    github_extractor = build_github_platform_extractor(
        cfg=cfg,
        db=db,
        scraper=scraper,
        response_formatter=response_formatter,
        audit_func=audit_func,
        sem=sem,
        quality_llm_client=llm_client,
        embedding_gen=embedding_gen,
        llm_repository=llm_repository,
    )
    suggester = build_star_list_suggester(
        app_cfg=cfg,
        database=db,
        embedding_service=embedding_service,
        vector_store=vector_store,
        llm_client=llm_client,
        llm_repository=llm_repository,
    )
    return AddRepositoryUseCase(
        ingest=GitHubRepositoryIngestAdapter(github_extractor),
        repository_repo=RepositoryReadRepositoryAdapter(db),
        star_lists=ManageStarListsUseCase(
            gateway_factory=GitHubGraphQLClient,
            repository_repo=RepositoryReadRepositoryAdapter(db),
            integration_repo=GitHubIntegrationRepository(db),
        ),
        suggester=suggester,
        mirrors=GitMirrorEnrollmentAdapter(GitMirrorRepository(db, cfg.git_backup)),
    )
