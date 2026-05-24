from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.time_utils import UTC
from app.mcp.aggregation_service import (
    AggregationMcpService,
    _build_failure_payload,
    _build_progress_payload,
)
from app.mcp.article_service import ArticleReadService
from app.mcp.catalog_service import CatalogReadService
from app.mcp.semantic_service import SemanticSearchService
from app.mcp.signal_service import SignalMcpService
from app.mcp.x_search_service import XSearchService


class _Result:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def __iter__(self) -> Any:
        return iter(self._rows)

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _Result:
        return self

    def mappings(self) -> _Result:
        return self


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result()

    async def scalars(self, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result()

    async def scalar(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


class _Database:
    def session(self) -> _Session:
        return _Session()


class _Context:
    user_id = 1

    def __init__(self) -> None:
        self.runtime = SimpleNamespace(
            database=_Database(),
            db=_Database(),
            cfg=SimpleNamespace(),
            background_processor=SimpleNamespace(),
            core=SimpleNamespace(),
        )

    def ensure_runtime(self) -> Any:
        return self.runtime

    async def ensure_api_runtime(self) -> Any:
        return self.runtime

    def request_scope_filters(self, *_args: Any) -> list[Any]:
        return []


def _dummy_value(name: str) -> Any:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    values = {
        "after": now,
        "before": now,
        "days": 7,
        "end_date": now,
        "items": [{"url": "https://example.test"}],
        "lang_preference": "auto",
        "limit": 5,
        "metadata": {},
        "offset": 0,
        "query": "ai #tag",
        "request_id": 1,
        "since": now,
        "source_id": 1,
        "start_date": now,
        "summary_id": 1,
        "text": "AI tools #tag",
        "topic": "ai",
    }
    if name in values:
        return values[name]
    if name.endswith("_id"):
        return 1
    if name.startswith(("include_", "is_")):
        return False
    if "limit" in name or "offset" in name:
        return 1
    if "date" in name or name.endswith("_at"):
        return now
    if name.endswith("s"):
        return []
    return "value"


def _arguments_for(method: Any) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for name, parameter in inspect.signature(method).parameters.items():
        if name == "self" or parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        args[name] = _dummy_value(name)
    return args


@pytest.mark.asyncio
async def test_mcp_services_exercise_empty_runtime_paths() -> None:
    context = _Context()
    article_service = ArticleReadService(context)  # type: ignore[arg-type]
    services = [
        article_service,
        AggregationMcpService(context),  # type: ignore[arg-type]
        CatalogReadService(context),  # type: ignore[arg-type]
        SemanticSearchService(context, article_service),  # type: ignore[arg-type]
        SignalMcpService(context),  # type: ignore[arg-type]
        XSearchService(context),  # type: ignore[arg-type]
    ]

    attempted = 0
    tolerated = 0
    for service in services:
        for name in dir(service):
            if name.startswith("_"):
                continue
            method = getattr(service, name)
            if not inspect.iscoroutinefunction(method):
                continue
            attempted += 1
            try:
                await method(**_arguments_for(method))
            except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
                tolerated += 1

    assert attempted >= 20
    assert tolerated < attempted


def test_mcp_service_helpers_are_deterministic() -> None:
    assert _build_progress_payload(
        {
            "total_items": 10,
            "successful_count": 2,
            "failed_count": 1,
            "duplicate_count": 1,
        }
    ) == {
        "total_items": 10,
        "processed_items": 4,
        "successful_count": 2,
        "failed_count": 1,
        "duplicate_count": 1,
        "completion_percent": 40,
    }
    assert _build_failure_payload({}) is None
    assert _build_failure_payload(
        {
            "failure_code": "bad",
            "failure_message": "Failed",
            "failure_details_json": {"field": "value"},
        }
    ) == {"code": "bad", "message": "Failed", "details": {"field": "value"}}

    semantic = SemanticSearchService(_Context(), ArticleReadService(_Context()))  # type: ignore[arg-type]
    assert semantic._tokenize("AI tools, ai") == {"ai", "tools"}
    assert semantic._lexical_overlap_score("ai tools", "ai only") == 0.5
    assert SemanticSearchService._cosine_similarity([1, 0], [1, 0]) == 1.0
    assert SemanticSearchService._cosine_similarity([], [1]) == 0.0
    assert SemanticSearchService._extract_query_tags("#AI #ai #Tools") == [
        "#ai",
        "#tools",
    ]
    assert (
        SemanticSearchService.extract_semantic_seed_text(
            {
                "metadata": {"title": "Title"},
                "summary_250": "Short",
                "tldr": "TLDR",
                "summary_1000": "Long",
                "key_ideas": ["Idea"],
                "topic_tags": ["tag"],
            }
        )
        == "Title Short TLDR Long Idea tag"
    )
