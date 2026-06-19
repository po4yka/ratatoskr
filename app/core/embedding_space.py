from __future__ import annotations

from typing import Any


def build_embedding_space_identifier(
    provider: str | None,
    *,
    model_name: str | None = None,
    dimensions: int | None = None,
) -> str | None:
    """Return a stable identifier for embedding spaces that need isolated indexes."""
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in {"gemini", "voyage"}:
        return None

    safe_model = _sanitize_identifier(model_name or "gemini")
    try:
        normalized_dimensions = int(dimensions) if dimensions is not None else None
    except (TypeError, ValueError):
        normalized_dimensions = None

    if normalized_dimensions is None or normalized_dimensions <= 0:
        return safe_model

    return f"{safe_model}_{normalized_dimensions}d"


def resolve_embedding_space_identifier(config: Any) -> str | None:
    """Resolve an embedding-space identifier from a config-like object."""
    if config is None:
        return None

    return build_embedding_space_identifier(
        getattr(config, "provider", None),
        model_name=getattr(config, "gemini_model", None)
        if getattr(config, "provider", None) == "gemini"
        else getattr(config, "voyage_model", None),
        dimensions=getattr(config, "gemini_dimensions", None)
        if getattr(config, "provider", None) == "gemini"
        else getattr(config, "voyage_dimensions", None),
    )


def _sanitize_identifier(value: str) -> str:
    cleaned = [
        char.lower() if char.isalnum() or char in {"-", "_"} else "_" for char in str(value).strip()
    ]
    normalized = "".join(cleaned).strip("_")
    return normalized or "embedding"
