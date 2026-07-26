"""Bot startup must supply the durable saver to the Telegram runtime."""

from __future__ import annotations

import importlib
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def test_bot_main_starts_checkpointer_before_constructing_runtime(monkeypatch) -> None:
    import bot as bot_module

    events: list[str] = []
    saver = object()

    config = SimpleNamespace(
        openrouter=SimpleNamespace(
            model="model",
            fallback_models=(),
            flash_model="flash",
            flash_fallback_models=(),
        ),
        model_routing=SimpleNamespace(
            enabled=False,
            default_model="model",
            technical_model="model",
            sociopolitical_model="model",
            long_context_model="model",
        ),
        attachment=SimpleNamespace(vision_model=None),
        runtime=SimpleNamespace(db_path="/data/ratatoskr.db"),
        langgraph_checkpoint=SimpleNamespace(enabled=True),
    )

    class FakeCheckpointerRuntime:
        def __init__(self, *, cfg: object) -> None:
            assert cfg is config
            self.saver = saver

        async def start(self) -> None:
            events.append("checkpointer_started")

        async def stop(self, *, timeout: float) -> None:
            assert timeout == 10.0
            events.append("checkpointer_stopped")

    class FakeQueue:
        def __init__(self, *, maxsize: int) -> None:
            assert maxsize == 256

        def start(self) -> None:
            events.append("queue_started")

        async def stop(self) -> None:
            events.append("queue_stopped")

    class FakeBot:
        def __init__(self, **kwargs: object) -> None:
            events.append("bot_constructed")
            builder = kwargs["runtime_builder"]
            assert isinstance(builder, partial)
            assert builder.func is bot_module.build_telegram_runtime
            assert builder.keywords == {"checkpointer": saver}
            self.message_handler = SimpleNamespace(
                message_router=SimpleNamespace(coalescer=SimpleNamespace(shutdown=AsyncMock()))
            )

        async def start(self) -> None:
            events.append("bot_started")

    database = SimpleNamespace(migrate=AsyncMock())
    broker = SimpleNamespace(is_worker_process=True)

    monkeypatch.setattr(bot_module, "load_config", lambda: config)
    monkeypatch.setattr(bot_module, "ConfigHolder", lambda cfg: cfg)
    monkeypatch.setattr(bot_module, "build_runtime_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(bot_module, "DbWriteQueue", FakeQueue)
    monkeypatch.setattr(bot_module, "TelegramBot", FakeBot)
    monkeypatch.setattr("app.observability.otel.init_tracing", lambda _cfg: None)
    monkeypatch.setattr(
        "app.infrastructure.checkpointing.CheckpointerRuntime", FakeCheckpointerRuntime
    )
    broker_module = importlib.import_module("app.tasks.broker")
    monkeypatch.setattr(broker_module, "broker", broker)

    await bot_module.main()

    assert events.index("checkpointer_started") < events.index("bot_constructed")
    assert events == [
        "queue_started",
        "checkpointer_started",
        "bot_constructed",
        "bot_started",
        "checkpointer_stopped",
        "queue_stopped",
    ]
