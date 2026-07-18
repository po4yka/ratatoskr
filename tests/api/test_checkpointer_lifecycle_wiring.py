"""FastAPI lifespan must supply the durable saver to the API runtime."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


async def test_api_runtime_threads_checkpointer_to_url_processor(monkeypatch) -> None:
    """The API composition root compiles its URL graph with the supplied saver."""
    from app.di import api as api_di

    saver = object()
    captured: dict[str, object] = {}
    config = SimpleNamespace(
        telegram=SimpleNamespace(allowed_user_ids=(1,)),
        web_search=SimpleNamespace(enabled=False),
    )
    database = SimpleNamespace(
        executor=MagicMock(),
        bootstrap=MagicMock(),
        maintenance=MagicMock(),
        inspection=MagicMock(),
        backups=MagicMock(),
    )
    core = SimpleNamespace(
        llm_client=MagicMock(),
        audit_sink=MagicMock(),
        firecrawl_client=MagicMock(),
        scraper_chain=MagicMock(),
        response_formatter=MagicMock(),
        semaphore_factory=MagicMock(),
    )
    search = SimpleNamespace(
        topic_searcher=MagicMock(),
        vector_store=MagicMock(),
        embedding_service=MagicMock(),
    )

    class StopAfterUrlProcessor(RuntimeError):
        pass

    def capture_url_processor(**kwargs: object) -> object:
        captured.update(kwargs)
        raise StopAfterUrlProcessor

    monkeypatch.setattr(api_di, "build_async_audit_sink", lambda _db: MagicMock())
    monkeypatch.setattr(api_di, "build_core_dependencies", lambda *_args, **_kwargs: core)
    monkeypatch.setattr(api_di, "build_search_dependencies", lambda *_args, **_kwargs: search)
    monkeypatch.setattr(api_di, "build_url_processor", capture_url_processor)

    with pytest.raises(StopAfterUrlProcessor):
        await api_di.build_api_runtime(
            config, db=database, redis_client=MagicMock(), checkpointer=saver
        )

    assert captured["checkpointer"] is saver
    assert captured["progress_event_repo"] is not None


async def test_lifespan_starts_checkpointer_before_building_api_runtime(monkeypatch) -> None:
    from app.api import main

    events: list[str] = []
    saver = object()

    class FakeCheckpointerRuntime:
        def __init__(self, *, cfg: object) -> None:
            assert cfg is config
            self.saver = saver

        async def start(self) -> None:
            events.append("checkpointer_started")

        async def stop(self, *, timeout: float) -> None:
            assert timeout == 10.0
            events.append("checkpointer_stopped")

    config = SimpleNamespace(
        sentry=SimpleNamespace(sentry_dsn=None),
        langgraph_checkpoint=SimpleNamespace(enabled=True),
    )
    runtime = SimpleNamespace(
        cfg=SimpleNamespace(
            runtime=SimpleNamespace(log_level="INFO", llm_provider="openrouter"),
            retention=SimpleNamespace(export_temp_file_max_age_seconds=0),
            background=SimpleNamespace(durable_worker_enabled=False),
            deployment=SimpleNamespace(
                status_total_timeout_seconds=1.0,
                status_cache_ttl_seconds=60.0,
                status_refresh_after_seconds=30.0,
            ),
            git_backup=SimpleNamespace(enabled=False),
            ai_backup=SimpleNamespace(enabled=False, chatgpt_enabled=False, claude_enabled=False),
        ),
        db=MagicMock(),
        durable_request_queue=SimpleNamespace(reconcile_startup=AsyncMock()),
        durable_transcription_queue=None,
    )

    async def build_runtime(*args: object, **kwargs: object) -> object:
        events.append("runtime_built")
        assert args == (config,)
        assert kwargs["checkpointer"] is saver
        return runtime

    monkeypatch.setattr("app.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr("app.observability.otel.init_tracing", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.infrastructure.checkpointing.CheckpointerRuntime", FakeCheckpointerRuntime
    )
    monkeypatch.setattr(main, "build_api_runtime", build_runtime)
    monkeypatch.setattr(main, "setup_json_logging", lambda _level: None)
    monkeypatch.setattr(main, "set_current_api_runtime", lambda _runtime: None)
    monkeypatch.setattr(main, "clear_current_api_runtime", lambda: None)
    monkeypatch.setattr(main, "close_api_runtime", AsyncMock())
    monkeypatch.setattr(main, "close_redis", AsyncMock())
    monkeypatch.setattr(
        "app.api.routers.auth.tokens.log_auth_posture_summary",
        lambda *_args, **_kwargs: None,
    )

    broker = SimpleNamespace(is_worker_process=True)
    broker_module = importlib.import_module("app.tasks.broker")
    monkeypatch.setattr(broker_module, "broker", broker)

    async with main.lifespan(MagicMock()):
        assert events == ["checkpointer_started", "runtime_built"]

    assert events == ["checkpointer_started", "runtime_built", "checkpointer_stopped"]


async def test_lifespan_import_error_falls_back_without_partial_runtime(monkeypatch) -> None:
    from app.api import main

    class StopAfterBuild(RuntimeError):
        pass

    class MissingDependencyRuntime:
        def __init__(self, *, cfg: object) -> None:
            pass

        async def start(self) -> None:
            raise ImportError("psycopg unavailable")

        @property
        def saver(self) -> object:
            raise AssertionError("partially started runtime must not be accessed")

    config = SimpleNamespace(
        sentry=SimpleNamespace(sentry_dsn=None),
        langgraph_checkpoint=SimpleNamespace(enabled=True),
    )

    async def build_runtime(*args: object, **kwargs: object) -> object:
        assert args == (config,)
        assert kwargs["checkpointer"] is None
        raise StopAfterBuild

    monkeypatch.setattr("app.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr("app.observability.otel.init_tracing", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.infrastructure.checkpointing.CheckpointerRuntime", MissingDependencyRuntime
    )
    monkeypatch.setattr(main, "build_api_runtime", build_runtime)
    monkeypatch.setattr(main, "close_redis", AsyncMock())
    monkeypatch.setattr("app.observability.metrics_http.mark_process_dead", lambda: None)

    with pytest.raises(StopAfterBuild):
        async with main.lifespan(MagicMock()):
            pass
