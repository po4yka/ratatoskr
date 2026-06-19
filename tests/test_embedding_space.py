from __future__ import annotations

from types import SimpleNamespace

from app.core.embedding_space import resolve_embedding_space_identifier


def test_embedding_space_is_none_for_local_provider() -> None:
    assert resolve_embedding_space_identifier(SimpleNamespace(provider="local")) is None


def test_embedding_space_includes_gemini_model_and_dimensions() -> None:
    config = SimpleNamespace(
        provider="gemini",
        gemini_model="gemini-embedding-2-preview",
        gemini_dimensions=768,
    )

    assert resolve_embedding_space_identifier(config) == "gemini-embedding-2-preview_768d"


def test_embedding_space_includes_voyage_model_and_dimensions() -> None:
    config = SimpleNamespace(
        provider="voyage",
        voyage_model="voyage-3-large",
        voyage_dimensions=1024,
    )

    assert resolve_embedding_space_identifier(config) == "voyage-3-large_1024d"
