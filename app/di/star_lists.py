"""Composition of the star-list suggester, shared by the API and the worker.

The API builds it to file a repository at the moment the user adds it; the Taskiq
filing job builds the same object to file repositories that were starred
somewhere else entirely -- the GitHub web UI, a phone, another script. Both paths
must reach the same verdict for the same repository, so the thresholds are read
from config here once instead of being passed in by each caller.

This module deliberately lives in ``app.di`` rather than ``app.di.api``: the
``tasks-no-api`` import contract forbids ``app.tasks`` from importing ``app.api``,
and a worker has no business dragging in the FastAPI runtime to build a
classifier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.db.session import Database


def build_star_list_suggester(
    *,
    app_cfg: AppConfig,
    database: Database,
    embedding_service: Any,
    vector_store: Any,
    llm_client: Any,
    llm_repository: Any = None,
) -> Any | None:
    """Build the suggester, or ``None`` when there is nothing to vote with.

    Returning ``None`` rather than a degraded object keeps the "no neighbours"
    case visible at the call site. A caller that cannot suggest has to decide
    whether to leave the repository unfiled, and that decision belongs to the
    caller: the API reports it as a warning on a response the user is waiting
    for, while the filing job just skips the row and tries again tomorrow.

    The optional collaborators degrade independently. Without a vector store
    there are no neighbours to vote with at all, so there is no suggester.
    Without the LLM fallback an inconclusive vote simply leaves the repository
    unfiled, which is the documented behaviour rather than a failure.
    """
    if vector_store is None:
        return None

    from app.application.services.star_list_classifier import StarListClassifierService
    from app.application.use_cases.suggest_star_lists import SuggestStarListsUseCase
    from app.infrastructure.search.repository_search_service import RepositorySearchService

    github_cfg = app_cfg.github

    classifier: Any = None
    if github_cfg.star_list_suggest_llm_fallback and llm_client is not None:
        classifier = StarListClassifierService(
            llm_client=llm_client,
            llm_repo=llm_repository,
        )

    return SuggestStarListsUseCase(
        neighbour_search=RepositorySearchService(
            embedding_service=embedding_service,
            qdrant_store=vector_store,
            db=database,
            environment=app_cfg.vector_store.environment,
            user_scope=app_cfg.vector_store.user_scope,
        ),
        classifier=classifier,
        neighbour_limit=github_cfg.star_list_suggest_k,
        min_similarity=github_cfg.star_list_suggest_min_similarity,
        min_score=github_cfg.star_list_suggest_min_score,
        dominance_ratio=github_cfg.star_list_suggest_dominance_ratio,
    )
