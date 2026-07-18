"""/setmodel hot-reload must reach the live OpenRouterClient, not just the snapshot.

The client freezes its model at construction, so ConfigHolder.swap now notifies
registered listeners; build_core_dependencies registers the client's
apply_runtime_config so a /setmodel reload actually takes effect without a
restart.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.openrouter.openrouter_client import OpenRouterClient
from app.config.config_holder import ConfigHolder

pytestmark = pytest.mark.no_network


def _client(model: str, fallback: list[str]) -> OpenRouterClient:
    client = OpenRouterClient.__new__(OpenRouterClient)
    client._model = model
    client._fallback_models = list(fallback)
    return client


def test_config_holder_notifies_listeners_on_swap() -> None:
    holder = ConfigHolder(SimpleNamespace(tag="old"))
    seen: list[str] = []
    holder.register_listener(lambda cfg: seen.append(cfg.tag))

    new = SimpleNamespace(tag="new")
    old = holder.swap(new)

    assert old.tag == "old"
    assert holder.cfg is new
    assert seen == ["new"]


def test_config_holder_listener_failure_is_isolated() -> None:
    holder = ConfigHolder(SimpleNamespace(tag="old"))
    calls: list[str] = []

    def _boom(_cfg: object) -> None:
        raise RuntimeError("listener blew up")

    holder.register_listener(_boom)
    holder.register_listener(lambda cfg: calls.append(cfg.tag))

    holder.swap(SimpleNamespace(tag="new"))

    # A failing listener must not abort the swap or block later listeners.
    assert holder.cfg.tag == "new"
    assert calls == ["new"]


def test_apply_runtime_config_updates_frozen_model() -> None:
    client = _client("old-model", ["old-fb"])
    new_cfg = SimpleNamespace(
        openrouter=SimpleNamespace(model="new-model", fallback_models=["fb-a", "fb-b"])
    )

    client.apply_runtime_config(new_cfg)

    assert client._model == "new-model"
    assert client._fallback_models == ["fb-a", "fb-b"]


def test_apply_runtime_config_noop_without_openrouter_section() -> None:
    client = _client("keep", ["keep-fb"])

    client.apply_runtime_config(SimpleNamespace())

    assert client._model == "keep"
    assert client._fallback_models == ["keep-fb"]


def test_setmodel_hot_reload_reaches_live_client() -> None:
    # End-to-end: swapping the holder propagates the new model to the client
    # that had frozen the old one at construction.
    client = _client("old-model", [])
    holder = ConfigHolder(
        SimpleNamespace(openrouter=SimpleNamespace(model="old-model", fallback_models=[]))
    )
    holder.register_listener(client.apply_runtime_config)

    holder.swap(
        SimpleNamespace(
            openrouter=SimpleNamespace(model="hot-swapped-model", fallback_models=["fb"])
        )
    )

    assert client._model == "hot-swapped-model"
    assert client._fallback_models == ["fb"]
