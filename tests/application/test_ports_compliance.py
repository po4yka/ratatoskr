"""Port compliance tests: verify factories return protocol-compliant adapters.

These tests assert that each repository factory in app/di/repositories.py
returns a concrete adapter that is recognised as an instance of its port
protocol (via @runtime_checkable isinstance checks) and that critical async
methods exist with the expected signatures.
"""

from __future__ import annotations

import inspect

import pytest

from app.application.ports import (
    AggregationSessionRepositoryPort,
    AuditLogRepositoryPort,
    BackupRepositoryPort,
    BatchSessionRepositoryPort,
    LLMRepositoryPort,
    RequestRepositoryPort,
    SocialConnectionRepositoryPort,
    SummaryRepositoryPort,
    UserRepositoryPort,
)
from app.application.ports.aggregation_sessions import (
    AggregationSessionRepositoryPort as AggregationSessionRepositoryPortDirect,
)
from app.application.ports.audit import AuditLogRepositoryPort as AuditLogRepositoryPortDirect
from app.application.ports.backups import BackupRepositoryPort as BackupRepositoryPortDirect
from app.application.ports.batch_sessions import (
    BatchSessionRepositoryPort as BatchSessionRepositoryPortDirect,
)
from app.application.ports.requests import (
    LLMRepositoryPort as LLMRepositoryPortDirect,
    RequestRepositoryPort as RequestRepositoryPortDirect,
)
from app.application.ports.social_connections import (
    SocialConnectionRepositoryPort as SocialConnectionRepositoryPortDirect,
)
from app.application.ports.summaries import SummaryRepositoryPort as SummaryRepositoryPortDirect
from app.application.ports.users import UserRepositoryPort as UserRepositoryPortDirect


@pytest.fixture
def db():
    return object()


def test_summary_repository_factory_returns_port_instance(db) -> None:
    from app.di.repositories import build_summary_repository

    repo = build_summary_repository(db)
    assert isinstance(repo, SummaryRepositoryPort)


def test_aggregation_session_repository_factory_returns_port_instance(db) -> None:
    from app.di.repositories import build_aggregation_session_repository

    repo = build_aggregation_session_repository(db)
    assert isinstance(repo, AggregationSessionRepositoryPort)


def test_request_repository_factory_returns_port_instance(db) -> None:
    from app.di.repositories import build_request_repository

    repo = build_request_repository(db)
    assert isinstance(repo, RequestRepositoryPort)


def test_llm_repository_factory_returns_port_instance(db) -> None:
    from app.di.repositories import build_llm_repository

    repo = build_llm_repository(db)
    assert isinstance(repo, LLMRepositoryPort)


def test_social_connection_repository_factory_returns_port_instance(db) -> None:
    from app.di.repositories import build_social_connection_repository

    repo = build_social_connection_repository(db)
    assert isinstance(repo, SocialConnectionRepositoryPort)


def test_summary_repository_critical_methods_are_async(db) -> None:
    from app.di.repositories import build_summary_repository

    repo = build_summary_repository(db)
    for method_name in (
        "async_get_user_summaries",
        "async_get_summary_context_by_id",
        "async_get_aggregation_source_bundle_for_summary",
    ):
        method = getattr(repo, method_name, None)
        assert method is not None, f"Missing method: {method_name}"
        assert inspect.iscoroutinefunction(method), f"{method_name} must be async"


def test_request_repository_critical_methods_are_async(db) -> None:
    from app.di.repositories import build_request_repository

    repo = build_request_repository(db)
    for method_name in ("async_create_request", "async_get_request_context"):
        method = getattr(repo, method_name, None)
        assert method is not None, f"Missing method: {method_name}"
        assert inspect.iscoroutinefunction(method), f"{method_name} must be async"


def test_root_facade_reexports_current_port_surface() -> None:
    assert AggregationSessionRepositoryPort is AggregationSessionRepositoryPortDirect
    assert AuditLogRepositoryPort is AuditLogRepositoryPortDirect
    assert BackupRepositoryPort is BackupRepositoryPortDirect
    assert BatchSessionRepositoryPort is BatchSessionRepositoryPortDirect
    assert LLMRepositoryPort is LLMRepositoryPortDirect
    assert RequestRepositoryPort is RequestRepositoryPortDirect
    assert SocialConnectionRepositoryPort is SocialConnectionRepositoryPortDirect
    assert SummaryRepositoryPort is SummaryRepositoryPortDirect
    assert UserRepositoryPort is UserRepositoryPortDirect


def test_port_submodules_import_cleanly() -> None:
    from app.application import ports
    from app.application.ports import (
        aggregation_sessions,
        audio,
        audit,
        backups,
        batch_sessions,
        imports,
        requests,
        rules,
        search,
        social_connections,
        summaries,
        users,
    )

    modules = (
        ports,
        aggregation_sessions,
        audit,
        audio,
        backups,
        batch_sessions,
        imports,
        requests,
        rules,
        search,
        social_connections,
        summaries,
        users,
    )

    assert all(module is not None for module in modules)
