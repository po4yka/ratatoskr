"""Coverage for the di/graphs assembly seam (audit #18).

Exercises the composition-root builders that wire the summarize graph + facade:

* ``build_summary_cache_adapter``: TTL from ``cfg.redis``, prompt_version from
  ``cfg.runtime``, environment/user_scope scope from ``cfg.vector_store``.
* ``build_model_router``: enabled -> a ``(tier, content_length) -> str`` lambda
  that delegates to ``resolve_model_for_content`` with ``has_images=False``;
  disabled -> ``None`` (conservative path).
* ``assemble_graph_url_processor`` with ``vector_store=None`` -> the facade's
  retrieval port is the ``_NullRetrievalPort`` stub (empty hits), so graph
  construction never depends on a live vector store.

All build under ``patch.dict(os.environ, MODEL_SELECTION_ENV, clear=True)`` so the
no-code-model-default contract (rule 11) is honored: the configs only validate
because MODEL_SELECTION_ENV supplies the otherwise-required model fields.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.dto.vector_search import EntityType, RetrievalScope
from tests._config_env import MODEL_SELECTION_ENV

# Minimum non-model env the AppConfig loader needs beyond MODEL_SELECTION_ENV.
_BASE_ENV = {
    **MODEL_SELECTION_ENV,
    "OPENROUTER_API_KEY": "sk-or-test-key-placeholder",
    "ALLOWED_USER_IDS": "1",
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/ratatoskr_test",
}


@contextmanager
def _loaded_config():
    """Build a real AppConfig under a cleared env (config cache reset around it)."""
    import app.config.settings as settings_mod

    with patch.dict(os.environ, _BASE_ENV, clear=True):
        with settings_mod._CONFIG_CACHE_LOCK:
            settings_mod._CONFIG_CACHE.clear()
        try:
            yield settings_mod.load_config(allow_stub_telegram=True)
        finally:
            with settings_mod._CONFIG_CACHE_LOCK:
                settings_mod._CONFIG_CACHE.clear()


# --------------------------------------------------------------------------- #
# build_summary_cache_adapter
# --------------------------------------------------------------------------- #


def test_build_summary_cache_adapter_sources_ttl_prompt_version_and_scope() -> None:
    from app.di.graphs import build_summary_cache_adapter

    fake_cache = MagicMock(enabled=True)
    with _loaded_config() as cfg:
        adapter = build_summary_cache_adapter(cfg, cache=fake_cache)

        assert adapter._cache is fake_cache  # injected cache is used (no Redis built)
        assert adapter._prompt_version == cfg.runtime.summary_prompt_version
        assert adapter._ttl_seconds == int(cfg.redis.llm_ttl_seconds)
        assert adapter._environment == (cfg.vector_store.environment or "dev")
        assert adapter._user_scope == (cfg.vector_store.user_scope or "public")
        # The key tuple carries the scope prefix sourced from cfg.vector_store.
        assert adapter._key_parts("en", "hash") == (
            "llm",
            cfg.vector_store.environment,
            cfg.vector_store.user_scope,
            cfg.runtime.summary_prompt_version,
            "en",
            "hash",
        )


# --------------------------------------------------------------------------- #
# build_model_router
# --------------------------------------------------------------------------- #


def test_build_model_router_returns_none_when_routing_disabled() -> None:
    from app.di.graphs import build_model_router

    with _loaded_config() as cfg:
        # Force the disabled path regardless of the loaded default.
        object.__setattr__(cfg.model_routing, "enabled", False)
        assert build_model_router(cfg) is None


def test_build_model_router_delegates_with_has_images_false() -> None:
    from app.core.content_classifier import ContentTier
    from app.di.graphs import build_model_router

    captured: dict[str, Any] = {}

    def _fake_resolve(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "routed/model"

    with _loaded_config() as cfg:
        object.__setattr__(cfg.model_routing, "enabled", True)
        # build_model_router lazily imports resolve_model_for_content INTO its closure,
        # so the patch must be active *while the router is built* (not just when called).
        with patch(
            "app.core.model_router.resolve_model_for_content",
            _fake_resolve,
        ):
            router = build_model_router(cfg)
            assert router is not None
            out = router(ContentTier.TECHNICAL, 4096)

        assert out == "routed/model"
        # has_images is hard-bound False (vision routing is resolved upstream in build_prompt).
        assert captured["has_images"] is False
        assert captured["tier"] is ContentTier.TECHNICAL
        assert captured["content_length"] == 4096
        assert captured["routing_config"] is cfg.model_routing
        assert captured["openrouter_config"] is cfg.openrouter


def test_build_model_router_resolves_real_tier_models() -> None:
    """End-to-end (no resolver patch): the router yields the configured tier models."""
    from app.core.content_classifier import ContentTier
    from app.di.graphs import build_model_router

    with _loaded_config() as cfg:
        object.__setattr__(cfg.model_routing, "enabled", True)
        router = build_model_router(cfg)
        assert router is not None
        # Short content -> DEFAULT tier resolves to the configured default model.
        assert router(ContentTier.DEFAULT, 100) == cfg.model_routing.default_model
        assert router(ContentTier.TECHNICAL, 100) == cfg.model_routing.technical_model


# --------------------------------------------------------------------------- #
# assemble_graph_url_processor -> NullRetrievalPort when vectors absent
# --------------------------------------------------------------------------- #


def test_null_retrieval_port_returns_empty_results() -> None:
    """The retrieval stub used when vectors are absent yields no hits."""
    import asyncio

    from app.di.graphs import _build_retrieval_port_or_stub

    port = _build_retrieval_port_or_stub(vector_store=None, embedding_service=None, db=MagicMock())

    scope = RetrievalScope(environment="test", user_scope="user", user_id=1)
    retrieve_result = asyncio.run(
        port.retrieve(entity_type=EntityType.SUMMARY, scope=scope, query="q", top_k=5)
    )
    similar_result = asyncio.run(
        port.find_similar(
            entity_type=EntityType.SUMMARY,
            entity_id="1",
            scope=scope,
            top_k=5,
        )
    )
    assert retrieve_result.hits == [] and retrieve_result.total == 0
    assert similar_result.hits == [] and similar_result.total == 0


def test_assemble_graph_url_processor_uses_null_retrieval_when_no_vectors() -> None:
    """assemble_graph_url_processor(vector_store=None) wires the null retrieval port.

    The langgraph compile (``build_summarize_graph_app``) is stubbed so the test
    needs no langgraph; we assert the facade's deps carry the empty-result stub.
    """
    import asyncio

    from app.di import graphs as graphs_mod
    from app.di.graphs import _NullRetrievalPort, assemble_graph_url_processor

    collaborators: dict[str, Any] = {
        "content_extractor": MagicMock(),
        "cached_summary_responder": MagicMock(maybe_reply=AsyncMock(return_value=None)),
        "summary_delivery": MagicMock(),
        "post_summary_tasks": MagicMock(),
        "response_formatter": MagicMock(),
        "audit_func": MagicMock(),
        "summarization_runtime": MagicMock(),
        "llm_client": MagicMock(),
        "request_repo": MagicMock(),
        "summary_repo": MagicMock(),
        "crawl_result_repo": MagicMock(),
        "llm_repo": MagicMock(),
    }

    with _loaded_config() as cfg:
        # Stub the langgraph compile + extraction-port wiring (adapter imports) so the
        # assembly runs without langgraph / a live extractor.
        with (
            patch.object(graphs_mod, "build_summarize_graph_app", return_value=MagicMock()),
            patch(
                "app.di.extraction.build_extraction_port",
                return_value=MagicMock(),
            ),
            patch.object(graphs_mod, "build_graph_url_processor") as build_facade,
        ):
            assemble_graph_url_processor(
                cfg=cfg,
                db=MagicMock(),
                vector_store=None,
                embedding_service=None,
                redis_cache=MagicMock(enabled=False),
                **collaborators,
            )

        # The facade builder received the deps carrying the null retrieval port.
        assert build_facade.call_count == 1
        deps = build_facade.call_args.kwargs["deps"]
        assert isinstance(deps.retrieval, _NullRetrievalPort)
        empty = asyncio.run(deps.retrieval.retrieve(query="q", top_k=3))
        assert empty.hits == [] and empty.total == 0


def test_telegram_runtime_threads_checkpointer_to_processing_stack(monkeypatch) -> None:
    """The Telegram composition root must pass the started saver to its URL graph."""
    from app.di import telegram as telegram_mod

    repositories = MagicMock()
    core = MagicMock()
    search = MagicMock()
    processing = MagicMock()
    interface = MagicMock()
    interface.durable_transcription_queue = None
    saver = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(telegram_mod, "_build_telegram_repositories", lambda _db: repositories)
    monkeypatch.setattr(telegram_mod, "VerbosityResolver", lambda _repo: MagicMock())
    monkeypatch.setattr(telegram_mod, "build_async_audit_sink", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(telegram_mod, "build_core_dependencies", lambda *_args, **_kwargs: core)
    monkeypatch.setattr(telegram_mod, "get_topic_search_limit", lambda _cfg: None)
    monkeypatch.setattr(telegram_mod, "_build_search_stack", lambda **_kwargs: search)
    monkeypatch.setattr(telegram_mod, "build_application_services", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(
        telegram_mod,
        "_build_processing_stack",
        lambda **kwargs: captured.update(kwargs) or processing,
    )
    monkeypatch.setattr(telegram_mod, "_build_telegram_interface_stack", lambda **_kwargs: interface)

    telegram_mod.build_telegram_runtime(
        MagicMock(),
        MagicMock(),
        safe_reply_func=MagicMock(),
        reply_json_func=MagicMock(),
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver


def test_telegram_processing_stack_threads_checkpointer_to_url_processor(monkeypatch) -> None:
    """The Telegram URL graph is compiled with the runtime's durable saver."""
    from types import SimpleNamespace

    from app.di import telegram as telegram_mod

    saver = object()
    captured: dict[str, object] = {}
    url_processor = MagicMock()
    url_processor.content_extractor = MagicMock()

    monkeypatch.setattr(
        telegram_mod,
        "_build_related_reads_service",
        lambda **_kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        telegram_mod,
        "build_url_processor",
        lambda **kwargs: captured.update(kwargs) or url_processor,
    )
    monkeypatch.setattr(telegram_mod, "ForwardProcessor", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(telegram_mod, "AttachmentProcessor", lambda **_kwargs: MagicMock())

    telegram_mod._build_processing_stack(
        cfg=SimpleNamespace(web_search=SimpleNamespace(enabled=False)),
        db=MagicMock(),
        core=MagicMock(),
        search=MagicMock(),
        repositories=MagicMock(),
        db_write_queue=None,
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver


@pytest.mark.parametrize("vectors_present", [True, False])
def test_build_summarize_config_routing_long_context_selection(vectors_present: bool) -> None:
    """build_summarize_config picks the routing long-context model iff routing on."""
    from app.di.graphs import build_summarize_config

    with _loaded_config() as cfg:
        object.__setattr__(cfg.model_routing, "enabled", vectors_present)
        sc = build_summarize_config(cfg)
        if vectors_present:
            assert sc.long_context_model == cfg.model_routing.long_context_model
            assert sc.routing_enabled is True
        else:
            assert sc.long_context_model == cfg.openrouter.long_context_model
            assert sc.routing_enabled is False
