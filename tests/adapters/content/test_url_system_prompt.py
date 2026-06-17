"""Coverage for the surviving ``get_url_system_prompt`` loader.

The legacy ``URLFlowContextBuilder`` + ``ContentChunker`` map-reduce path was
deleted at the T9 cutover (audit #21); ``get_url_system_prompt`` is the one
export that remained (the pre-extracted background handler still uses it).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.adapters.content.url_flow_context_builder import get_url_system_prompt


def test_get_url_system_prompt_delegates_to_prompt_manager() -> None:
    manager = MagicMock()
    manager.get_system_prompt.return_value = "SYSTEM PROMPT EN"
    with patch(
        "app.adapters.content.url_flow_context_builder.get_prompt_manager",
        return_value=manager,
    ):
        out = get_url_system_prompt("en")
    assert out == "SYSTEM PROMPT EN"
    manager.get_system_prompt.assert_called_once_with("en", include_examples=True, num_examples=2)


def test_get_url_system_prompt_falls_back_on_manager_error() -> None:
    with patch(
        "app.adapters.content.url_flow_context_builder.get_prompt_manager",
        side_effect=RuntimeError("prompt store unavailable"),
    ):
        out = get_url_system_prompt("ru")
    # Hardened fallback: a strict-JSON instruction rather than a crash.
    assert "strict JSON object" in out
