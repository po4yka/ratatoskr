"""Tests for task runtime dependency bundles."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.di.tasks import DigestTaskRuntime, RssPollTaskRuntime


def test_digest_task_runtime_invokes_configured_factories() -> None:
    cfg = SimpleNamespace(name="cfg")
    calls: list[tuple[str, Any]] = []

    def userbot_factory(received_cfg: Any) -> str:
        calls.append(("userbot", received_cfg))
        return "userbot"

    def llm_client_factory(received_cfg: Any) -> str:
        calls.append(("llm", received_cfg))
        return "llm"

    def bot_client_factory(received_cfg: Any) -> str:
        calls.append(("bot", received_cfg))
        return "bot"

    def service_factory(received_cfg: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("service", received_cfg))
        return kwargs

    runtime = DigestTaskRuntime(
        cfg=cfg,  # type: ignore[arg-type]
        userbot_factory=userbot_factory,
        llm_client_factory=llm_client_factory,
        bot_client_factory=bot_client_factory,
        service_factory=service_factory,
    )

    assert runtime.create_userbot() == "userbot"
    assert runtime.create_llm_client() == "llm"
    assert runtime.create_bot_client() == "bot"
    assert runtime.create_service(userbot="u", llm_client="l", send_message="s") == {
        "userbot": "u",
        "llm_client": "l",
        "send_message": "s",
    }
    assert calls == [
        ("userbot", cfg),
        ("llm", cfg),
        ("bot", cfg),
        ("service", cfg),
    ]


def test_rss_poll_task_runtime_invokes_configured_factories() -> None:
    cfg = SimpleNamespace(name="cfg")
    db = SimpleNamespace(name="db")
    calls: list[tuple[str, Any, Any | None]] = []

    def bot_client_factory(received_cfg: Any) -> str:
        calls.append(("bot", received_cfg, None))
        return "bot"

    def delivery_service_factory(received_cfg: Any, received_db: Any) -> str:
        calls.append(("delivery", received_cfg, received_db))
        return "delivery"

    def signal_worker_factory(received_cfg: Any, received_db: Any) -> str:
        calls.append(("signal", received_cfg, received_db))
        return "signal"

    def source_runner_factory(received_cfg: Any, received_db: Any) -> str:
        calls.append(("source", received_cfg, received_db))
        return "source"

    runtime = RssPollTaskRuntime(
        cfg=cfg,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        bot_client_factory=bot_client_factory,
        delivery_service_factory=delivery_service_factory,
        signal_worker_factory=signal_worker_factory,
        source_runner_factory=source_runner_factory,
    )

    assert runtime.create_bot_client() == "bot"
    assert runtime.create_delivery_service() == "delivery"
    assert runtime.create_signal_ingestion_worker() == "signal"
    assert runtime.create_source_ingestion_runner() == "source"
    assert calls == [
        ("bot", cfg, None),
        ("delivery", cfg, db),
        ("signal", cfg, db),
        ("source", cfg, db),
    ]


def test_task_deps_digest_runtime_uses_delegated_factories(monkeypatch) -> None:
    import app.di.tasks as di_tasks
    from app.tasks import deps

    cfg = SimpleNamespace()
    # build_digest_task_runtime is re-exported from di.tasks; patch there.
    monkeypatch.setattr(
        di_tasks, "create_digest_userbot", lambda received_cfg: ("userbot", received_cfg)
    )
    monkeypatch.setattr(
        di_tasks, "create_digest_llm_client", lambda received_cfg: ("llm", received_cfg)
    )
    monkeypatch.setattr(
        di_tasks, "create_digest_bot_client", lambda received_cfg: ("bot", received_cfg)
    )
    monkeypatch.setattr(
        di_tasks,
        "create_digest_service",
        lambda received_cfg, **kwargs: ("service", received_cfg, kwargs),
    )

    runtime = deps.build_digest_task_runtime(cfg)  # type: ignore[arg-type]

    assert runtime.create_userbot() == ("userbot", cfg)
    assert runtime.create_llm_client() == ("llm", cfg)
    assert runtime.create_bot_client() == ("bot", cfg)
    assert runtime.create_service(userbot="u", llm_client="l", send_message="s") == (
        "service",
        cfg,
        {"userbot": "u", "llm_client": "l", "send_message": "s"},
    )
