"""Database access helpers backed by the shared API runtime."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import Depends

from app.application.use_cases.search_read_model import SearchReadModelUseCase
from app.application.use_cases.summary_read_model import SummaryReadModelUseCase
from app.core.logging_utils import get_logger
from app.di.database import clear_cached_runtime_database, get_or_create_runtime_database_from_env

def _unset() -> None:
    """FastAPI sub-dependency that always resolves to ``None``.

    Defined above the TYPE_CHECKING block on purpose: the ``else`` branch below
    evaluates at runtime and references it.
    """


if TYPE_CHECKING:
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
    )
    from app.application.ports.search import TopicSearchRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.db.session import Database

    # Type-checker view: real Database class. Runtime view: Any.
    # FastAPI inspects these dep-callable signatures via `get_type_hints` and
    # would try to build a Pydantic JSON schema for `Database` (a non-Pydantic
    # class), which fails. Erasing the type at runtime makes FastAPI treat the
    # param as opaque while preserving type-checker fidelity for callers.
    DatabaseDep = Database
    SessionManagerDep = Database | None
    OpaqueDep = Any
else:
    DatabaseDep = Any
    # Annotated[..., Depends(_unset)] is what keeps these OUT of the public
    # contract. FastAPI walks a dependency's own parameters, and any one that
    # is not a Depends and not a recognised non-field type (Request, Response,
    # BackgroundTasks, ...) becomes a request field — defaulting to a QUERY
    # parameter. `session_manager: Any | None` and `request: Any` therefore
    # advertised themselves on every operation that injects one of these
    # providers: 27 operations carried a bogus `?session_manager=&request=`.
    # Marking them as sub-dependencies that always resolve to None keeps the
    # internal call sites working (they still pass values positionally in
    # tests and in app wiring) while telling FastAPI they are not inputs.
    SessionManagerDep = Annotated[Any, Depends(_unset)]
    OpaqueDep = Annotated[Any, Depends(_unset)]


logger = get_logger(__name__)


def resolve_api_runtime(request: OpaqueDep = None) -> Any:
    """Resolve the API runtime through a patchable module-level wrapper."""
    from app.di.api import resolve_api_runtime as _resolve_api_runtime

    return _resolve_api_runtime(request)


def get_session_manager(request: OpaqueDep = None) -> Database:
    """Resolve the shared API database facade."""
    try:
        return cast("Database", resolve_api_runtime(request).db)
    except RuntimeError:
        manager = get_or_create_runtime_database_from_env(migrate=True)
        logger.info(
            "session_manager_initialized",
            extra={"database_dsn": _redact_dsn(manager.config.dsn)},
        )
        return manager


def clear_session_manager() -> None:
    """Reset API runtime and fallback DB state used in tests."""
    with contextlib.suppress(Exception):
        from app.di.api import get_current_api_runtime

        runtime = get_current_api_runtime()
        database = getattr(runtime.db, "dispose", None)
        if database is not None:
            # clear_cached_runtime_database disposes the fallback DB; runtime-owned
            # databases are disposed by the FastAPI lifespan once O3 ports it.
            pass
    with contextlib.suppress(Exception):
        from app.di.api import clear_current_api_runtime

        clear_current_api_runtime()
    clear_cached_runtime_database()


def resolve_repository_session(
    session_manager: DatabaseDep | Any | None = None,
    request: OpaqueDep = None,
) -> Database | Any:
    """Resolve the DB handle repositories should bind to."""
    if session_manager is not None:
        return session_manager

    with contextlib.suppress(RuntimeError):
        return resolve_api_runtime(request).db

    return get_session_manager(request)


def get_request_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> RequestRepositoryPort:
    """Build a request repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.request_repository import (
        RequestRepositoryAdapter,
    )

    return RequestRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_summary_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> SummaryRepositoryPort:
    """Build a summary repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.summary_repository import (
        SummaryRepositoryAdapter,
    )

    return SummaryRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_crawl_result_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> CrawlResultRepositoryPort:
    """Build a crawl-result repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.crawl_result_repository import (
        CrawlResultRepositoryAdapter,
    )

    return CrawlResultRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_llm_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> LLMRepositoryPort:
    """Build an LLM repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.llm_repository import (
        LLMRepositoryAdapter,
    )

    return LLMRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_user_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> UserRepositoryPort:
    """Build a user repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.user_repository import (
        UserRepositoryAdapter,
    )

    return UserRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_auth_repository(
    token_cache: OpaqueDep = None,
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build an auth repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.auth_repository import (
        AuthRepositoryAdapter,
    )

    return AuthRepositoryAdapter(
        resolve_repository_session(session_manager, request),
        token_cache=token_cache,
    )


def get_user_credential_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build a user-credentials repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.user_credentials_repository import (
        UserCredentialRepositoryAdapter,
    )

    return UserCredentialRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_collection_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build a collection repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.collection_repository import (
        CollectionRepositoryAdapter,
    )

    return CollectionRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_device_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build a device repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.device_repository import (
        DeviceRepositoryAdapter,
    )

    return DeviceRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_backup_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build a backup repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.backup_repository import (
        BackupRepositoryAdapter,
    )

    return BackupRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_rule_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build a rule repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.rule_repository import (
        RuleRepositoryAdapter,
    )

    return RuleRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_webhook_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build a webhook repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.webhook_repository import (
        WebhookRepositoryAdapter,
    )

    return WebhookRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_import_job_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build an import-job repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.import_job_repository import (
        ImportJobRepositoryAdapter,
    )

    return ImportJobRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_bookmark_import_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build a bookmark-import repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.bookmark_import_repository import (
        BookmarkImportAdapter,
    )

    return BookmarkImportAdapter(resolve_repository_session(session_manager, request))


def get_audio_generation_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> Any:
    """Build an audio-generation repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.audio_generation_repository import (
        AudioGenerationRepositoryAdapter,
    )

    return AudioGenerationRepositoryAdapter(resolve_repository_session(session_manager, request))


def get_topic_search_repository(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> TopicSearchRepositoryPort:
    """Build a topic-search repository bound to the shared session manager."""
    from app.infrastructure.persistence.repositories.topic_search_repository import (
        TopicSearchRepositoryAdapter,
    )

    return TopicSearchRepositoryAdapter(resolve_repository_session(session_manager, request))


def _resolve_db(
    session_manager: DatabaseDep | Any | None,
    request: Any,
    runtime_attr: str,
) -> Any:
    """Return an object from the API runtime, falling back to a direct DB session.

    Used by the read-model factory functions below to avoid repeating the
    ``if session_manager / suppress(RuntimeError) / fallback`` pattern.
    Returns the resolved object when ``runtime_attr`` is empty-string (i.e.
    the caller wants just the DB session manager), or the named attribute from
    the API runtime when the runtime is available.
    """
    if session_manager is not None:
        return resolve_repository_session(session_manager, request)
    if runtime_attr:
        with contextlib.suppress(RuntimeError):
            from app.di.api import resolve_api_runtime as _resolve

            return getattr(_resolve(request), runtime_attr)
    return resolve_repository_session(None, request)


def get_summary_read_model_use_case(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> SummaryReadModelUseCase:
    """Resolve the shared summary read-model use case from API runtime."""
    resolved = _resolve_db(session_manager, request, "summary_read_model_use_case")
    if isinstance(resolved, SummaryReadModelUseCase):
        return resolved
    # resolved is a Database session manager — build the use case from repos
    manager = resolved
    return SummaryReadModelUseCase(
        summary_repository=get_summary_repository(manager, request),
        request_repository=get_request_repository(manager, request),
        crawl_result_repository=get_crawl_result_repository(manager, request),
        llm_repository=get_llm_repository(manager, request),
    )


def get_search_read_model_use_case(
    session_manager: SessionManagerDep = None,
    request: OpaqueDep = None,
) -> SearchReadModelUseCase:
    """Resolve the shared search read-model use case from API runtime."""
    resolved = _resolve_db(session_manager, request, "search_read_model_use_case")
    if isinstance(resolved, SearchReadModelUseCase):
        return resolved
    manager = resolved
    return SearchReadModelUseCase(
        topic_search_repository=get_topic_search_repository(manager, request),
        request_repository=get_request_repository(manager, request),
        summary_repository=get_summary_repository(manager, request),
    )


def _redact_dsn(dsn: str) -> str:
    if "@" not in dsn:
        return dsn
    prefix, suffix = dsn.rsplit("@", 1)
    if ":" not in prefix:
        return f"...@{suffix}"
    scheme_user, _password = prefix.rsplit(":", 1)
    return f"{scheme_user}:***@{suffix}"
