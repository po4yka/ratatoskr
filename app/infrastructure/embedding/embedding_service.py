"""Service for generating and managing semantic embeddings for articles."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from app.application.ports.search import EmbeddingDependencyUnavailableError
from app.core.logging_utils import get_logger
from app.infrastructure.embedding.embedding_protocol import EmbeddingSerializationMixin
from app.observability.attributes import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
)
from app.observability.metrics import record_db_query

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sentence_transformers import SentenceTransformer

logger = get_logger(__name__)


def _get_tracer() -> object:
    from app.observability.otel import get_tracer

    return get_tracer(__name__)


# Language-specific model configuration
# Maps language codes to optimal embedding models
DEFAULT_MODELS = {
    "en": "all-MiniLM-L6-v2",  # English-optimized, 384 dims
    "ru": "paraphrase-multilingual-MiniLM-L12-v2",  # Multilingual, good for Russian, 384 dims
    "auto": "paraphrase-multilingual-MiniLM-L12-v2",  # Default multilingual model, 384 dims
}


class EmbeddingService(EmbeddingSerializationMixin):
    """Generate and manage semantic embeddings for articles with multi-language support."""

    def __init__(
        self,
        default_model: str = "paraphrase-multilingual-MiniLM-L12-v2",
        model_registry: dict[str, str] | None = None,
    ) -> None:
        """Initialize embedding service with multi-language support.

        Args:
            default_model: Default model to use when language is not specified
            model_registry: Custom mapping of language codes to model names
                           If None, uses DEFAULT_MODELS
        """
        self._default_model = default_model
        self._model_registry = model_registry or DEFAULT_MODELS.copy()
        self._models: dict[str, SentenceTransformer] = {}  # Model cache per language
        self._dimensions: dict[str, int] = {}  # Dimensions per model
        # Cached hard-dependency failure (e.g. torch/CUDA libs missing). Once
        # the sentence-transformers import fails, re-attempting it per call is
        # both pointless and slow, so the failure is remembered and re-raised.
        self._dependency_error: EmbeddingDependencyUnavailableError | None = None

    def _get_model_name_for_language(self, language: str | None) -> str:
        """Get the appropriate model name for a language."""
        if not language:
            return self._default_model

        # Check registry
        return self._model_registry.get(language, self._default_model)

    def _ensure_model(self, model_name: str) -> SentenceTransformer:
        """Lazy load the embedding model (cached per model name).

        Raises:
            EmbeddingDependencyUnavailableError: when the sentence-transformers
                / torch backend cannot be imported on this host. The failure is
                cached so subsequent calls fail fast instead of retrying the
                expensive (and doomed) import.
        """
        if self._dependency_error is not None:
            raise self._dependency_error

        if model_name not in self._models:
            try:
                from sentence_transformers import SentenceTransformer
            except (ImportError, OSError, ValueError) as exc:
                # torch surfaces missing CUDA libs as OSError/ValueError during
                # import; treat any of these as a hard, non-transient outage.
                self._dependency_error = EmbeddingDependencyUnavailableError(
                    f"sentence-transformers backend unavailable: {exc}"
                )
                raise self._dependency_error from exc

            model = SentenceTransformer(model_name)
            self._models[model_name] = model
            # Read the dimension from the model's reported config rather than a
            # probe encode("test") forward pass. Fall back to a probe only if the
            # backend cannot report it.
            dim = model.get_sentence_embedding_dimension()
            if not dim:
                dim = len(model.encode("test"))
            self._dimensions[model_name] = dim
            logger.info(
                "embedding_model_loaded",
                extra={"model": model_name, "dims": self._dimensions[model_name]},
            )
        return self._models[model_name]

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> Any:
        """Generate embedding vector for text.

        Args:
            text: Text to embed
            language: Language code (en, ru, auto) to select optimal model

        Returns:
            Numpy array embedding vector
        """
        model_name = self._get_model_name_for_language(language)
        model = self._ensure_model(model_name)
        dims = self._dimensions.get(model_name, 0)

        with _get_tracer().start_as_current_span("embedding.encode") as span:
            span.set_attribute(EMBEDDING_MODEL, model_name)
            span.set_attribute(EMBEDDING_BATCH_SIZE, 1)
            span.set_attribute(EMBEDDING_DIMS, dims)
            t0 = time.monotonic()
            result = await asyncio.to_thread(
                model.encode, text, convert_to_numpy=True, show_progress_bar=False
            )
            record_db_query("embedding_encode_single", time.monotonic() - t0)
        return result

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[Any]:
        """Generate embeddings for multiple texts using native batched encode."""
        model_name = self._get_model_name_for_language(language)
        model = self._ensure_model(model_name)
        dims = self._dimensions.get(model_name, 0)
        batch_size = len(texts)

        with _get_tracer().start_as_current_span("embedding.encode_batch") as span:
            span.set_attribute(EMBEDDING_MODEL, model_name)
            span.set_attribute(EMBEDDING_BATCH_SIZE, batch_size)
            span.set_attribute(EMBEDDING_DIMS, dims)
            t0 = time.monotonic()
            raw = await asyncio.to_thread(
                model.encode,
                list(texts),
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            record_db_query("embedding_encode_batch", time.monotonic() - t0)
        return list(raw)

    def get_model_name(self, language: str | None = None) -> str:
        """Get model name for a specific language."""
        return self._get_model_name_for_language(language)

    def get_dimensions(self, language: str | None = None) -> int:
        """Get embedding dimensions for a specific language.

        Loads the model if not already loaded.
        """
        model_name = self._get_model_name_for_language(language)
        if model_name not in self._dimensions:
            self._ensure_model(model_name)
        return self._dimensions[model_name]

    def close(self) -> None:
        """Release cached models and clear state."""
        for model in self._models.values():
            try:
                # Ensure model is moved off GPU if used; ignore if unsupported
                if hasattr(model, "to"):
                    model.to("cpu")
            except Exception:  # pragma: no cover - defensive cleanup
                logger.exception(
                    "embedding_model_close_failed", extra={"model": getattr(model, "name", None)}
                )
        self._models.clear()
        self._dimensions.clear()

    async def aclose(self) -> None:
        """Async wrapper for close()."""
        await asyncio.to_thread(self.close)
